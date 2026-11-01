package com.smartthings.cleaner

import android.app.DatePickerDialog
import android.app.TimePickerDialog
import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.smartthings.cleaner.model.CancelReservationResult
import com.smartthings.cleaner.model.FetchReservationsResult
import com.smartthings.cleaner.model.ReservationItem
import com.smartthings.cleaner.model.UpdateReservationResult
import com.smartthings.cleaner.network.ServerClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * 1회성 청소 예약 목록 액티비티.
 *
 * SPEC:
 * - REQ-002: 예약 목록 조회 (예정된 예약과 지난 예약의 날짜·시각과 상태 표시)
 * - REQ-003: 예약 일시 수정 (예정 예약만 수정 가능, 실행/완료된 예약 수정 불가)
 * - REQ-004: 예약 취소 (예정 예약만 취소 가능, 실행/완료된 예약 취소 불가)
 * - REQ-009: 실패 이유 및 원인 미확인/마지막 기기 상태 표시
 * - REQ-010: 두 사용자 모두 조회/수정/취소 가능
 * - REQ-025: 서버 연결 불가 경고
 * - REQ-028 / REQ-029: S21+ 및 Fold8(접힘/펼침 반응형) 지원
 * - must: 다섯 가지 상태(예정·성공·실패·취소·누락)를 모두 구분해 표시한다.
 * - must: 이미 실행된 예약은 수정할 수 없다.
 * - must: 이미 실행된 예약은 취소할 수 없다.
 */
class ReservationListActivity : AppCompatActivity() {

    data class StatusStyle(val label: String, val textColorRes: Int, val bgColorRes: Int, val strokeColorRes: Int)

    companion object {
        fun getStatusStyle(status: String): StatusStyle = when (status) {
            ReservationItem.STATUS_SCHEDULED -> StatusStyle("예정", R.color.status_scheduled, R.color.status_scheduled_bg, R.color.badge_border_scheduled)
            ReservationItem.STATUS_SUCCESS -> StatusStyle("성공", R.color.status_success, R.color.status_success_bg, R.color.badge_border_success)
            ReservationItem.STATUS_FAILED -> StatusStyle("실패", R.color.status_failed, R.color.status_failed_bg, R.color.badge_border_failed)
            ReservationItem.STATUS_CANCELLED -> StatusStyle("취소", R.color.status_cancelled, R.color.status_cancelled_bg, R.color.badge_border_cancelled)
            ReservationItem.STATUS_MISSED -> StatusStyle("누락", R.color.status_missed, R.color.status_missed_bg, R.color.badge_border_missed)
            else -> StatusStyle(status, R.color.text_secondary, R.color.status_disconnected_bg, R.color.border_color)
        }

        fun partitionReservations(reservations: List<ReservationItem>): Pair<List<ReservationItem>, List<ReservationItem>> =
            Pair(reservations.filter { it.isUpcoming() }, reservations.filter { !it.isUpcoming() })

        fun formatFailureReasonText(reason: String?, lastDeviceState: String?): String = when {
            !reason.isNullOrBlank() && reason != "원인 미확인" -> "실패 이유: $reason"
            !lastDeviceState.isNullOrBlank() -> "실패 이유: 원인 미확인 (마지막 기기 상태: $lastDeviceState)"
            !reason.isNullOrBlank() -> "실패 이유: $reason"
            else -> "실패 이유: 원인 미확인"
        }
    }

    private val serverClient = ServerClient()
    private lateinit var btnRefresh: MaterialButton
    private lateinit var btnAddReservation: MaterialButton
    private lateinit var progressBar: ProgressBar
    private lateinit var cardError: MaterialCardView
    private lateinit var textErrorMessage: TextView
    private lateinit var textUpcomingHeader: TextView
    private lateinit var textUpcomingEmpty: TextView
    private lateinit var layoutUpcomingList: LinearLayout
    private lateinit var textPastHeader: TextView
    private lateinit var textPastEmpty: TextView
    private lateinit var layoutPastList: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_reservation_list)
        initViews()
        loadReservations()
    }

    override fun onResume() {
        super.onResume()
        loadReservations()
    }

    private fun initViews() {
        btnRefresh = findViewById(R.id.btnRefresh)
        btnAddReservation = findViewById(R.id.btnAddReservation)
        progressBar = findViewById(R.id.progressBar)
        cardError = findViewById(R.id.cardError)
        textErrorMessage = findViewById(R.id.textErrorMessage)
        textUpcomingHeader = findViewById(R.id.textUpcomingHeader)
        textUpcomingEmpty = findViewById(R.id.textUpcomingEmpty)
        layoutUpcomingList = findViewById(R.id.layoutUpcomingList)
        textPastHeader = findViewById(R.id.textPastHeader)
        textPastEmpty = findViewById(R.id.textPastEmpty)
        layoutPastList = findViewById(R.id.layoutPastList)

        btnRefresh.setOnClickListener { loadReservations() }
        btnAddReservation.setOnClickListener {
            val (serverUrl, sharedSecret) = getCredentials()
            startActivity(Intent(this, CreateReservationActivity::class.java).apply {
                putExtra("EXTRA_SERVER_URL", serverUrl)
                putExtra("EXTRA_SHARED_SECRET", sharedSecret)
            })
        }
    }

    private fun getCredentials(): Pair<String, String> = Pair(
        intent.getStringExtra("EXTRA_SERVER_URL") ?: ServerClient.getSavedServerUrl(this),
        intent.getStringExtra("EXTRA_SHARED_SECRET") ?: ServerClient.getSavedSharedSecret(this)
    )

    fun loadReservations() {
        val (serverUrl, sharedSecret) = getCredentials()
        if (serverUrl.isBlank() || sharedSecret.isBlank()) {
            showError("서버 주소와 비밀키가 설정되지 않았습니다. 메인 화면에서 먼저 서버에 연결해 주세요.")
            return
        }
        progressBar.visibility = View.VISIBLE
        btnRefresh.isEnabled = false
        cardError.visibility = View.GONE
        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.fetchReservations(serverUrl, sharedSecret)
            withContext(Dispatchers.Main) {
                progressBar.visibility = View.GONE
                btnRefresh.isEnabled = true
                handleFetchResult(result)
            }
        }
    }

    fun handleFetchResult(result: FetchReservationsResult) {
        when (result) {
            is FetchReservationsResult.Success -> {
                cardError.visibility = View.GONE
                renderReservations(result.reservations)
            }
            is FetchReservationsResult.Failure -> {
                val errorMsg = when (result.statusCode) {
                    401 -> "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)"
                    0 -> "서버에 연결할 수 없습니다. 서버 주소와 네트워크를 확인하세요. (REQ-025)"
                    else -> result.message
                }
                showError(errorMsg)
            }
        }
    }

    private fun showError(message: String) {
        cardError.visibility = View.VISIBLE
        textErrorMessage.text = message
    }

    fun renderReservations(reservations: List<ReservationItem>) {
        val (upcoming, past) = partitionReservations(reservations)
        renderSection(textUpcomingHeader, textUpcomingEmpty, layoutUpcomingList, upcoming, getString(R.string.section_upcoming_reservations))
        renderSection(textPastHeader, textPastEmpty, layoutPastList, past, getString(R.string.section_past_reservations))
    }

    private fun renderSection(header: TextView, emptyView: TextView, container: LinearLayout, items: List<ReservationItem>, title: String) {
        header.text = "$title (${items.size}건)"
        container.removeAllViews()
        emptyView.visibility = if (items.isEmpty()) View.VISIBLE else View.GONE
        container.visibility = if (items.isEmpty()) View.GONE else View.VISIBLE
        if (items.isNotEmpty()) {
            val inflater = LayoutInflater.from(this)
            for (item in items) {
                val itemView = inflater.inflate(R.layout.item_reservation, container, false)
                bindItemView(itemView, item)
                container.addView(itemView)
            }
        }
    }

    fun bindItemView(view: View, item: ReservationItem) {
        val textScheduledAt = view.findViewById<TextView>(R.id.textScheduledAt)
        val textStatusBadge = view.findViewById<TextView>(R.id.textStatusBadge)
        val cardStatusBadge = view.findViewById<MaterialCardView>(R.id.cardStatusBadge)
        val textFailureReason = view.findViewById<TextView>(R.id.textFailureReason)
        val textReservationId = view.findViewById<TextView>(R.id.textReservationId)
        val textOffPeriodNotice = view.findViewById<TextView>(R.id.textOffPeriodNotice)
        val btnEditReservation = view.findViewById<MaterialButton?>(R.id.btnEditReservation)
        val btnCancelReservation = view.findViewById<MaterialButton?>(R.id.btnCancelReservation)

        textScheduledAt.text = item.formatKstDisplay()
        textReservationId.text = "ID: ${item.id}"

        val style = getStatusStyle(item.status)
        textStatusBadge.text = style.label
        textStatusBadge.setTextColor(ContextCompat.getColor(this, style.textColorRes))
        cardStatusBadge.setCardBackgroundColor(ContextCompat.getColor(this, style.bgColorRes))
        cardStatusBadge.strokeColor = ContextCompat.getColor(this, style.strokeColorRes)

        if (item.status == ReservationItem.STATUS_FAILED) {
            textFailureReason.visibility = View.VISIBLE
            textFailureReason.text = formatFailureReasonText(item.failureReason, item.lastDeviceState)
        } else {
            textFailureReason.visibility = View.GONE
        }

        textOffPeriodNotice.visibility = if (item.allowDuringOffPeriod) View.VISIBLE else View.GONE
        if (item.allowDuringOffPeriod) textOffPeriodNotice.text = "쉬는 기간 허용"

        // MUST: 이미 실행된 예약은 수정/취소할 수 없다 (item.isUpcoming()일 때만 수정/취소 버튼 노출)
        val isUp = item.isUpcoming()
        btnEditReservation?.let {
            it.visibility = if (isUp) View.VISIBLE else View.GONE
            it.setOnClickListener(if (isUp) View.OnClickListener { showEditPickers(item) } else null)
        }
        btnCancelReservation?.let {
            it.visibility = if (isUp) View.VISIBLE else View.GONE
            it.setOnClickListener(if (isUp) View.OnClickListener { showCancelConfirmDialog(item) } else null)
        }
    }

    fun showCancelConfirmDialog(item: ReservationItem) {
        AlertDialog.Builder(this)
            .setTitle("예약 취소")
            .setMessage("예약을 취소하시겠습니까?")
            .setPositiveButton("확인") { _, _ -> performCancelReservation(item.id) }
            .setNegativeButton("닫기", null)
            .show()
    }

    fun performCancelReservation(reservationId: String) {
        val (serverUrl, sharedSecret) = getCredentials()
        if (serverUrl.isBlank() || sharedSecret.isBlank()) return showError("서버 주소와 비밀키가 설정되지 않았습니다.")
        progressBar.visibility = View.VISIBLE
        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.cancelReservation(serverUrl, sharedSecret, reservationId)
            withContext(Dispatchers.Main) {
                progressBar.visibility = View.GONE
                when (result) {
                    is CancelReservationResult.Success -> {
                        Toast.makeText(this@ReservationListActivity, result.message, Toast.LENGTH_SHORT).show()
                        loadReservations()
                    }
                    is CancelReservationResult.Failure -> showError(result.message)
                }
            }
        }
    }

    fun showEditPickers(item: ReservationItem) {
        val kstZone = ZoneId.of("Asia/Seoul")
        val initialDateTime = try {
            ZonedDateTime.parse(item.scheduledAt).withZoneSameInstant(kstZone)
        } catch (_: Exception) {
            ZonedDateTime.now(kstZone).plusDays(1).withHour(10).withMinute(0)
        }
        var chosenDate = initialDateTime.toLocalDate()
        DatePickerDialog(this, { _, year, month, dayOfMonth ->
            chosenDate = LocalDate.of(year, month + 1, dayOfMonth)
            TimePickerDialog(this, { _, hourOfDay, minute ->
                val newDt = ZonedDateTime.of(chosenDate, LocalTime.of(hourOfDay, minute), kstZone)
                if (!newDt.isAfter(ZonedDateTime.now(kstZone))) return@TimePickerDialog showError("과거 시각은 예약할 수 없습니다.")
                val isoStr = newDt.format(DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:00+09:00", Locale.ROOT))
                performUpdateReservation(item.id, isoStr)
            }, initialDateTime.hour, initialDateTime.minute, true).show()
        }, chosenDate.year, chosenDate.monthValue - 1, chosenDate.dayOfMonth).show()
    }

    fun performUpdateReservation(reservationId: String, newScheduledAt: String) {
        val (serverUrl, sharedSecret) = getCredentials()
        if (serverUrl.isBlank() || sharedSecret.isBlank()) return showError("서버 주소와 비밀키가 설정되지 않았습니다.")
        progressBar.visibility = View.VISIBLE
        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.updateReservation(serverUrl, sharedSecret, reservationId, newScheduledAt)
            withContext(Dispatchers.Main) {
                progressBar.visibility = View.GONE
                when (result) {
                    is UpdateReservationResult.Success -> {
                        Toast.makeText(this@ReservationListActivity, result.message, Toast.LENGTH_SHORT).show()
                        loadReservations()
                    }
                    is UpdateReservationResult.Failure -> showError(result.message)
                }
            }
        }
    }
}
