package com.smartthings.cleaner

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.smartthings.cleaner.model.*
import com.smartthings.cleaner.network.ServerClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.temporal.ChronoUnit
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * 1회성 청소 예약 생성 액티비티.
 *
 * SPEC:
 * - done_when: 앱에서 날짜·시각을 골라 저장하면 확인 메시지가 뜨고,
 *              과거 시각을 고르면 저장되지 않고 안내가 보인다
 * - SPEC F-1: 2. "예약 추가" -> 날짜·시각 선택 -> 저장
 * - D-03: 모드·구역 선택 없음 (기본 전체 청소만 지원)
 * - D-09 / REQ-024: 공유 비밀키 인증
 * - REQ-001: 분 단위 미래 일시 KST 지정
 * - REQ-028 / REQ-029: S21+ 및 Fold8(접힘/펼침) 반응형 지원
 * - LB-049: 일시는 공용 피커(DateTimeWheelPicker: 달력+시간 휠)로 선택한다. 날짜/시각 선택 버튼·미리보기 카드 없음.
 *   예약 수정(ReservationListActivity)도 EXTRA_PICK_ONLY 모드로 이 화면을 재사용한다: 서버 호출 없이
 *   선택한 KST ISO 일시를 RESULT_SCHEDULED_AT 로 돌려주고, 쉬는 기간 경고·수정 호출은 호출한 목록 화면이 기존대로 수행한다.
 */
class CreateReservationActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_PICK_ONLY = "EXTRA_PICK_ONLY"
        const val EXTRA_SCHEDULED_AT = "EXTRA_SCHEDULED_AT"
        const val RESULT_SCHEDULED_AT = "RESULT_SCHEDULED_AT"
        val KST_ZONE: ZoneId = ZoneId.of("Asia/Seoul")
        val DISPLAY_FORMATTER: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyy년 MM월 dd일 HH:mm ('KST')", Locale.KOREAN)
        val ISO_FORMATTER: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:00+09:00", Locale.ROOT)

        sealed class ValidationState {
            object Valid : ValidationState()
            data class Past(val message: String) : ValidationState()
        }

        fun checkDateTimeValidity(
            selected: ZonedDateTime,
            currentKst: ZonedDateTime = ZonedDateTime.now(KST_ZONE)
        ): ValidationState {
            return if (!selected.isAfter(currentKst)) {
                ValidationState.Past("과거 시각은 예약할 수 없습니다.")
            } else {
                ValidationState.Valid
            }
        }

        fun formatIsoKst(dateTime: ZonedDateTime): String {
            return dateTime.format(ISO_FORMATTER)
        }

        fun isDateTimeInOffPeriods(
            dateTime: ZonedDateTime,
            offPeriods: List<OffPeriodItem>
        ): Boolean = OffPeriodItem.isDateTimeInOffPeriods(dateTime, offPeriods)
    }

    private val serverClient = ServerClient()

    private lateinit var picker: DateTimeWheelPicker
    private var pickOnly = false

    private lateinit var cardValidationWarning: MaterialCardView
    private lateinit var textValidationMessage: TextView

    private lateinit var cardConfirmation: MaterialCardView
    private lateinit var textConfirmationMessage: TextView
    private lateinit var textConfirmationDetail: TextView

    private lateinit var btnSaveReservation: MaterialButton
    private lateinit var progressBarSave: ProgressBar
    private lateinit var btnCancel: MaterialButton

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_create_reservation)

        initViews()
        setDefaultDateTime()
        updateDateTimePreview()
        if (pickOnly) {
            findViewById<TextView>(R.id.textTitle).text = getString(R.string.title_edit_reservation)
            btnSaveReservation.text = getString(R.string.btn_save_edit_reservation)
        }
    }

    private fun initViews() {
        picker = findViewById(R.id.reservationPicker)
        picker.onValueChanged = { updateDateTimePreview() }
        pickOnly = intent.getBooleanExtra(EXTRA_PICK_ONLY, false)

        cardValidationWarning = findViewById(R.id.cardValidationWarning)
        textValidationMessage = findViewById(R.id.textValidationMessage)

        cardConfirmation = findViewById(R.id.cardConfirmation)
        textConfirmationMessage = findViewById(R.id.textConfirmationMessage)
        textConfirmationDetail = findViewById(R.id.textConfirmationDetail)

        btnSaveReservation = findViewById(R.id.btnSaveReservation)
        progressBarSave = findViewById(R.id.progressBarSave)
        btnCancel = findViewById(R.id.btnCancel)

        btnSaveReservation.setOnClickListener { performSaveReservation() }
        btnCancel.setOnClickListener { finish() }
    }

    private fun setDefaultDateTime() {
        // 기존 규칙: 선택 일시는 현재보다 뒤여야 한다(checkDateTimeValidity). 피커 최소값 = 다음 분.
        val nowKst = ZonedDateTime.now(KST_ZONE)
        picker.minDateTime = nowKst.truncatedTo(ChronoUnit.MINUTES).plusMinutes(1)
        val initial = intent.getStringExtra(EXTRA_SCHEDULED_AT)?.let {
            try { ZonedDateTime.parse(it).withZoneSameInstant(KST_ZONE) } catch (_: Exception) { null }
        }
        picker.value = initial ?: nowKst.toLocalDate().plusDays(1).atTime(10, 0).atZone(KST_ZONE)
    }

    fun getSelectedZonedDateTime(): ZonedDateTime = picker.value

    fun setSelectedDateTime(dateTime: ZonedDateTime) {
        picker.value = dateTime.withZoneSameInstant(KST_ZONE)
        updateDateTimePreview()
    }

    fun updateDateTimePreview() {
        val zdt = getSelectedZonedDateTime()

        val validity = checkDateTimeValidity(zdt)
        if (validity is ValidationState.Past) {
            // 과거 시각을 고르면 저장되지 않고 안내가 보인다 (done_when)
            cardValidationWarning.visibility = View.VISIBLE
            textValidationMessage.text = getString(R.string.msg_error_past_datetime)
            cardConfirmation.visibility = View.GONE
        } else {
            cardValidationWarning.visibility = View.GONE
        }
    }

    private fun performSaveReservation() {
        val zdt = getSelectedZonedDateTime()
        val validity = checkDateTimeValidity(zdt)
        if (validity is ValidationState.Past) {
            cardValidationWarning.visibility = View.VISIBLE
            textValidationMessage.text = getString(R.string.msg_error_past_datetime)
            cardConfirmation.visibility = View.GONE
            return
        }

        if (pickOnly) {
            setResult(RESULT_OK, Intent().putExtra(RESULT_SCHEDULED_AT, formatIsoKst(zdt)))
            finish()
            return
        }

        val serverUrl = intent.getStringExtra("EXTRA_SERVER_URL") ?: ServerClient.getSavedServerUrl(this)
        val sharedSecret = intent.getStringExtra("EXTRA_SHARED_SECRET") ?: ServerClient.getSavedSharedSecret(this)
        if (serverUrl.isBlank() || sharedSecret.isBlank()) {
            cardValidationWarning.visibility = View.VISIBLE
            textValidationMessage.text = getString(R.string.msg_error_server_not_configured)
            return
        }

        setSaving(true)
        lifecycleScope.launch(Dispatchers.IO) {
            val offPeriodsRes = serverClient.fetchOffPeriods(serverUrl, sharedSecret)
            val offPeriods = if (offPeriodsRes is FetchOffPeriodsResult.Success) offPeriodsRes.offPeriods else emptyList()
            val insideOffPeriod = isDateTimeInOffPeriods(zdt, offPeriods)

            withContext(Dispatchers.Main) {
                if (insideOffPeriod) {
                    setSaving(false)
                    showOffPeriodWarningDialog(
                        onConfirm = { executeCreateReservation(serverUrl, sharedSecret, zdt, true) },
                        onCancel = { /* 취소: 저장하지 않음 (Done When) */ }
                    )
                } else {
                    executeCreateReservation(serverUrl, sharedSecret, zdt, false)
                }
            }
        }
    }

    fun showOffPeriodWarningDialog(onConfirm: () -> Unit, onCancel: () -> Unit = {}) {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.title_off_period_warning))
            .setMessage(getString(R.string.msg_off_period_reservation_warning))
            .setPositiveButton(getString(R.string.btn_ok)) { _, _ -> onConfirm() }
            .setNegativeButton(getString(R.string.btn_cancel)) { dialog, _ ->
                dialog.dismiss()
                onCancel()
            }
            .setCancelable(false)
            .show()
    }

    private fun executeCreateReservation(
        serverUrl: String,
        sharedSecret: String,
        zdt: ZonedDateTime,
        allowDuringOffPeriod: Boolean,
    ) {
        setSaving(true)
        lifecycleScope.launch(Dispatchers.IO) {
            val isoString = formatIsoKst(zdt)
            val result = serverClient.createReservation(serverUrl, sharedSecret, isoString, allowDuringOffPeriod)
            withContext(Dispatchers.Main) {
                setSaving(false)
                when (result) {
                    is CreateReservationResult.Success -> {
                        cardValidationWarning.visibility = View.GONE
                        cardConfirmation.visibility = View.VISIBLE
                        textConfirmationMessage.text = getString(R.string.msg_confirm_reservation_created)
                        textConfirmationDetail.text =
                            "예약 ID: ${result.id}\n상태: ${result.status}\n일시: ${result.scheduledAt}"
                        showConfirmationDialog(getString(R.string.msg_confirm_reservation_created))
                    }
                    is CreateReservationResult.Failure -> {
                        cardValidationWarning.visibility = View.VISIBLE
                        textValidationMessage.text = result.message
                        cardConfirmation.visibility = View.GONE
                    }
                }
            }
        }
    }

    private fun setSaving(isSaving: Boolean) {
        btnSaveReservation.isEnabled = !isSaving
        btnSaveReservation.text = if (isSaving) {
            getString(R.string.btn_saving_reservation)
        } else {
            getString(R.string.btn_save_reservation)
        }
        progressBarSave.visibility = if (isSaving) View.VISIBLE else View.GONE
        picker.isEnabled = !isSaving
    }

    private fun showConfirmationDialog(message: String) {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.title_confirm))
            .setMessage(message)
            .setPositiveButton(getString(R.string.btn_ok)) { dialog, _ ->
                dialog.dismiss()
                setResult(RESULT_OK)
                finish()
            }
            .setCancelable(false)
            .show()
    }
}
