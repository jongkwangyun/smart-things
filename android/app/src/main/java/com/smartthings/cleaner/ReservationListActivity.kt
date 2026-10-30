package com.smartthings.cleaner

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.smartthings.cleaner.model.FetchReservationsResult
import com.smartthings.cleaner.model.ReservationItem
import com.smartthings.cleaner.network.ServerClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 1회성 청소 예약 목록 액티비티.
 *
 * SPEC:
 * - done_when: 앱 목록에 예정 예약과 지난 예약이 일시와 상태(예정·성공·실패·취소·누락)와 함께 보인다
 * - REQ-002: 예약 목록 조회 (예정된 예약과 지난 예약의 날짜·시각과 상태 표시)
 * - REQ-009: 실패 이유 및 원인 미확인/마지막 기기 상태 표시
 * - REQ-010: 두 사용자 모두 조회 가능
 * - REQ-025: 서버 연결 불가 경고
 * - REQ-028 / REQ-029: S21+ 및 Fold8(접힘/펼침 반응형) 지원
 * - must: 다섯 가지 상태(예정·성공·실패·취소·누락)를 모두 구분해 표시한다.
 */
class ReservationListActivity : AppCompatActivity() {

    data class StatusStyle(
        val label: String,
        val textColorRes: Int,
        val bgColorRes: Int,
        val strokeColorRes: Int
    )

    companion object {
        fun getStatusStyle(status: String): StatusStyle {
            return when (status) {
                ReservationItem.STATUS_SCHEDULED -> StatusStyle(
                    label = "예정",
                    textColorRes = R.color.status_scheduled,
                    bgColorRes = R.color.status_scheduled_bg,
                    strokeColorRes = R.color.badge_border_scheduled
                )
                ReservationItem.STATUS_SUCCESS -> StatusStyle(
                    label = "성공",
                    textColorRes = R.color.status_success,
                    bgColorRes = R.color.status_success_bg,
                    strokeColorRes = R.color.badge_border_success
                )
                ReservationItem.STATUS_FAILED -> StatusStyle(
                    label = "실패",
                    textColorRes = R.color.status_failed,
                    bgColorRes = R.color.status_failed_bg,
                    strokeColorRes = R.color.badge_border_failed
                )
                ReservationItem.STATUS_CANCELLED -> StatusStyle(
                    label = "취소",
                    textColorRes = R.color.status_cancelled,
                    bgColorRes = R.color.status_cancelled_bg,
                    strokeColorRes = R.color.badge_border_cancelled
                )
                ReservationItem.STATUS_MISSED -> StatusStyle(
                    label = "누락",
                    textColorRes = R.color.status_missed,
                    bgColorRes = R.color.status_missed_bg,
                    strokeColorRes = R.color.badge_border_missed
                )
                else -> StatusStyle(
                    label = status,
                    textColorRes = R.color.text_secondary,
                    bgColorRes = R.color.status_disconnected_bg,
                    strokeColorRes = R.color.border_color
                )
            }
        }

        fun partitionReservations(
            reservations: List<ReservationItem>
        ): Pair<List<ReservationItem>, List<ReservationItem>> {
            val upcoming = reservations.filter { it.isUpcoming() }
            val past = reservations.filter { !it.isUpcoming() }
            return Pair(upcoming, past)
        }

        fun formatFailureReasonText(reason: String?, lastDeviceState: String?): String {
            return when {
                !reason.isNullOrBlank() -> "실패 이유: $reason"
                !lastDeviceState.isNullOrBlank() -> "실패 이유: 원인 미확인 (마지막 기기 상태: $lastDeviceState)"
                else -> "실패 이유: 원인 미확인"
            }
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

        btnRefresh.setOnClickListener {
            loadReservations()
        }

        btnAddReservation.setOnClickListener {
            val serverUrl = intent.getStringExtra("EXTRA_SERVER_URL")
                ?: ServerClient.getSavedServerUrl(this)
            val sharedSecret = intent.getStringExtra("EXTRA_SHARED_SECRET")
                ?: ServerClient.getSavedSharedSecret(this)

            val createIntent = Intent(this, CreateReservationActivity::class.java).apply {
                putExtra("EXTRA_SERVER_URL", serverUrl)
                putExtra("EXTRA_SHARED_SECRET", sharedSecret)
            }
            startActivity(createIntent)
        }
    }

    fun loadReservations() {
        val serverUrl = intent.getStringExtra("EXTRA_SERVER_URL")
            ?: ServerClient.getSavedServerUrl(this)
        val sharedSecret = intent.getStringExtra("EXTRA_SHARED_SECRET")
            ?: ServerClient.getSavedSharedSecret(this)

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

        // 1. 예정된 예약 렌더링
        textUpcomingHeader.text = getString(R.string.section_upcoming_reservations) + " (${upcoming.size}건)"
        layoutUpcomingList.removeAllViews()
        if (upcoming.isEmpty()) {
            textUpcomingEmpty.visibility = View.VISIBLE
            layoutUpcomingList.visibility = View.GONE
        } else {
            textUpcomingEmpty.visibility = View.GONE
            layoutUpcomingList.visibility = View.VISIBLE
            val inflater = LayoutInflater.from(this)
            for (item in upcoming) {
                val itemView = inflater.inflate(R.layout.item_reservation, layoutUpcomingList, false)
                bindItemView(itemView, item)
                layoutUpcomingList.addView(itemView)
            }
        }

        // 2. 지난 예약 렌더링
        textPastHeader.text = getString(R.string.section_past_reservations) + " (${past.size}건)"
        layoutPastList.removeAllViews()
        if (past.isEmpty()) {
            textPastEmpty.visibility = View.VISIBLE
            layoutPastList.visibility = View.GONE
        } else {
            textPastEmpty.visibility = View.GONE
            layoutPastList.visibility = View.VISIBLE
            val inflater = LayoutInflater.from(this)
            for (item in past) {
                val itemView = inflater.inflate(R.layout.item_reservation, layoutPastList, false)
                bindItemView(itemView, item)
                layoutPastList.addView(itemView)
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

        textScheduledAt.text = item.formatKstDisplay()
        textReservationId.text = "ID: ${item.id}"

        // MUST: 5가지 상태를 모두 구분해 표시한다
        val style = getStatusStyle(item.status)
        textStatusBadge.text = style.label
        textStatusBadge.setTextColor(ContextCompat.getColor(this, style.textColorRes))
        cardStatusBadge.setCardBackgroundColor(ContextCompat.getColor(this, style.bgColorRes))
        cardStatusBadge.strokeColor = ContextCompat.getColor(this, style.strokeColorRes)

        // 실패 상태일 때만 실패 이유 표시 (REQ-009)
        if (item.status == ReservationItem.STATUS_FAILED) {
            textFailureReason.visibility = View.VISIBLE
            textFailureReason.text = formatFailureReasonText(item.failureReason, item.lastDeviceState)
        } else {
            textFailureReason.visibility = View.GONE
        }

        // 쉬는 기간 허용 여부 플래그
        if (item.allowDuringOffPeriod) {
            textOffPeriodNotice.visibility = View.VISIBLE
            textOffPeriodNotice.text = "쉬는 기간 허용"
        } else {
            textOffPeriodNotice.visibility = View.GONE
        }
    }
}
