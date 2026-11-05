package com.smartthings.cleaner

import android.os.Bundle
import android.view.View
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.smartthings.cleaner.model.CreateOffPeriodResult
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
 * 쉬는 기간 등록 액티비티.
 *
 * SPEC:
 * - done_when: 앱에서 시작·종료 일시를 골라 저장하면 확인 메시지가 뜨고,
 *              종료가 시작보다 앞서면 저장되지 않고 안내가 보인다
 * - SPEC F-3: 1. "쉬는 기간" -> 시작·종료 일시 선택 -> 저장
 * - D-09 / REQ-024: 공유 비밀키 인증
 * - REQ-011: 시작 일시와 종료 일시 지정, 종료 > 시작 필수
 * - LB-050: 시작/종료 탭으로 공용 피커(DateTimeWheelPicker 범위 달력 모드)를 전환, 종료 <= 시작이면 즉시 경고+저장 차단
 * - REQ-028 / REQ-029: S21+ 및 Fold8(접힘/펼침) 반응형 지원
 */
class CreateOffPeriodActivity : AppCompatActivity() {

    companion object {
        val KST_ZONE: ZoneId = ZoneId.of("Asia/Seoul")
        val DISPLAY_FORMATTER: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm", Locale.KOREAN)
        val ISO_FORMATTER: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:00+09:00", Locale.ROOT)

        sealed class ValidationState {
            object Valid : ValidationState()
            data class InvalidRange(val message: String) : ValidationState()
        }

        fun checkDateTimeValidity(
            startZdt: ZonedDateTime,
            endZdt: ZonedDateTime
        ): ValidationState {
            return if (!endZdt.isAfter(startZdt)) {
                ValidationState.InvalidRange("종료 일시는 시작 일시보다 뒤여야 합니다.")
            } else {
                ValidationState.Valid
            }
        }

        fun formatIsoKst(dateTime: ZonedDateTime): String {
            return dateTime.format(ISO_FORMATTER)
        }
    }

    private val serverClient = ServerClient()

    private var startDate: LocalDate = LocalDate.now(KST_ZONE).plusDays(1)
    private var startTime: LocalTime = LocalTime.of(9, 0)
    private var endDate: LocalDate = LocalDate.now(KST_ZONE).plusDays(3)
    private var endTime: LocalTime = LocalTime.of(18, 0)

    private lateinit var picker: DateTimeWheelPicker
    private lateinit var cardStartTab: MaterialCardView
    private lateinit var cardEndTab: MaterialCardView
    private lateinit var textDurationSummary: TextView
    private var editingStart = true

    private lateinit var btnPickStartDate: MaterialButton
    private lateinit var textSelectedStartDate: TextView
    private lateinit var btnPickStartTime: MaterialButton
    private lateinit var textSelectedStartTime: TextView
    private lateinit var btnPickEndDate: MaterialButton
    private lateinit var textSelectedEndDate: TextView
    private lateinit var btnPickEndTime: MaterialButton
    private lateinit var textSelectedEndTime: TextView
    private lateinit var textSelectedRangePreview: TextView
    private lateinit var textStartIsoPreview: TextView
    private lateinit var textEndIsoPreview: TextView
    private lateinit var cardValidationWarning: MaterialCardView
    private lateinit var textValidationMessage: TextView
    private lateinit var cardConfirmation: MaterialCardView
    private lateinit var textConfirmationMessage: TextView
    private lateinit var textConfirmationDetail: TextView
    private lateinit var btnSaveOffPeriod: MaterialButton
    private lateinit var progressBarSave: ProgressBar
    private lateinit var btnCancel: MaterialButton

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_create_off_period)

        initViews()
        setDefaultDateTime()
        selectTab(isStart = true)
    }

    private fun initViews() {
        btnPickStartDate = findViewById(R.id.btnPickStartDate)
        textSelectedStartDate = findViewById(R.id.textSelectedStartDate)
        btnPickStartTime = findViewById(R.id.btnPickStartTime)
        textSelectedStartTime = findViewById(R.id.textSelectedStartTime)

        btnPickEndDate = findViewById(R.id.btnPickEndDate)
        textSelectedEndDate = findViewById(R.id.textSelectedEndDate)
        btnPickEndTime = findViewById(R.id.btnPickEndTime)
        textSelectedEndTime = findViewById(R.id.textSelectedEndTime)

        textSelectedRangePreview = findViewById(R.id.textSelectedRangePreview)
        textStartIsoPreview = findViewById(R.id.textStartIsoPreview)
        textEndIsoPreview = findViewById(R.id.textEndIsoPreview)

        cardValidationWarning = findViewById(R.id.cardValidationWarning)
        textValidationMessage = findViewById(R.id.textValidationMessage)

        cardConfirmation = findViewById(R.id.cardConfirmation)
        textConfirmationMessage = findViewById(R.id.textConfirmationMessage)
        textConfirmationDetail = findViewById(R.id.textConfirmationDetail)

        btnSaveOffPeriod = findViewById(R.id.btnSaveOffPeriod)
        progressBarSave = findViewById(R.id.progressBarSave)
        btnCancel = findViewById(R.id.btnCancel)

        picker = findViewById(R.id.offPeriodPicker)
        cardStartTab = findViewById(R.id.cardStartDateSelection)
        cardEndTab = findViewById(R.id.cardEndDateSelection)
        textDurationSummary = findViewById(R.id.textDurationSummary)
        picker.rangeCalendarEnabled = true
        // 선택값은 탭마다 유지된다: 피커 변경은 현재 탭의 값에만 반영되고, 탭 전환 시 그 탭의 값으로 피커를 되돌린다.
        picker.onValueChanged = { z -> if (editingStart) setStartDateTime(z) else setEndDateTime(z) }
        cardStartTab.setOnClickListener { selectTab(isStart = true) }
        cardEndTab.setOnClickListener { selectTab(isStart = false) }

        btnSaveOffPeriod.setOnClickListener { performSaveOffPeriod() }
        btnCancel.setOnClickListener { finish() }
    }

    private fun setDefaultDateTime() {
        val nowKst = ZonedDateTime.now(KST_ZONE)
        startDate = nowKst.toLocalDate().plusDays(1)
        startTime = LocalTime.of(9, 0)
        endDate = nowKst.toLocalDate().plusDays(3)
        endTime = LocalTime.of(18, 0)
    }

    fun getStartDateTime(): ZonedDateTime = ZonedDateTime.of(startDate, startTime, KST_ZONE)
    fun getEndDateTime(): ZonedDateTime = ZonedDateTime.of(endDate, endTime, KST_ZONE)

    fun setStartDateTime(dateTime: ZonedDateTime) {
        val kst = dateTime.withZoneSameInstant(KST_ZONE)
        startDate = kst.toLocalDate()
        startTime = LocalTime.of(kst.hour, kst.minute)
        updateDateTimePreview()
    }

    fun setEndDateTime(dateTime: ZonedDateTime) {
        val kst = dateTime.withZoneSameInstant(KST_ZONE)
        endDate = kst.toLocalDate()
        endTime = LocalTime.of(kst.hour, kst.minute)
        updateDateTimePreview()
    }

    /** 시작/종료 탭 전환. 같은 피커에 해당 탭의 저장된 값을 채운다(리스너 호출 없음). */
    private fun selectTab(isStart: Boolean) {
        editingStart = isStart
        picker.value = if (isStart) getStartDateTime() else getEndDateTime()
        updateDateTimePreview()
    }

    fun updateDateTimePreview() {
        val startZdt = getStartDateTime()
        val endZdt = getEndDateTime()

        textSelectedStartDate.text = String.format(Locale.KOREAN, "%04d-%02d-%02d", startZdt.year, startZdt.monthValue, startZdt.dayOfMonth)
        textSelectedStartTime.text = String.format(Locale.KOREAN, "%02d:%02d", startZdt.hour, startZdt.minute)
        textSelectedEndDate.text = String.format(Locale.KOREAN, "%04d-%02d-%02d", endZdt.year, endZdt.monthValue, endZdt.dayOfMonth)
        textSelectedEndTime.text = String.format(Locale.KOREAN, "%02d:%02d", endZdt.hour, endZdt.minute)

        textSelectedRangePreview.text = "${startZdt.format(DISPLAY_FORMATTER)} ~ ${endZdt.format(DISPLAY_FORMATTER)}"
        textStartIsoPreview.text = "시작 ISO: ${formatIsoKst(startZdt)}"
        textEndIsoPreview.text = "종료 ISO: ${formatIsoKst(endZdt)}"

        picker.setRange(startZdt, endZdt)
        textDurationSummary.text = getString(R.string.label_off_period_duration, RangeCalendarLogic.durationText(startZdt, endZdt))
        textDurationSummary.visibility = if (RangeCalendarLogic.isValidRange(startZdt, endZdt)) View.VISIBLE else View.GONE
        for ((card, active) in listOf(cardStartTab to editingStart, cardEndTab to !editingStart)) {
            card.strokeWidth = if (active) 6 else 2
            card.strokeColor = getColor(if (active) R.color.primary else R.color.badge_border_cancelled)
            card.setCardBackgroundColor(getColor(if (active) R.color.status_scheduled_bg else R.color.surface))
        }
        refreshSaveEnabled(isSaving = false)

        val validity = checkDateTimeValidity(startZdt, endZdt)
        if (validity is ValidationState.InvalidRange) {
            cardValidationWarning.visibility = View.VISIBLE
            textValidationMessage.text = getString(R.string.msg_error_invalid_off_period_range)
            cardConfirmation.visibility = View.GONE
        } else {
            cardValidationWarning.visibility = View.GONE
        }
    }

    private fun performSaveOffPeriod() {
        val startZdt = getStartDateTime()
        val endZdt = getEndDateTime()
        val validity = checkDateTimeValidity(startZdt, endZdt)

        // 종료가 시작보다 앞서거나 같으면 저장되지 않고 안내가 보인다 (done_when)
        if (validity is ValidationState.InvalidRange) {
            cardValidationWarning.visibility = View.VISIBLE
            textValidationMessage.text = getString(R.string.msg_error_invalid_off_period_range)
            cardConfirmation.visibility = View.GONE
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
            val startIso = formatIsoKst(startZdt)
            val endIso = formatIsoKst(endZdt)
            val result = serverClient.createOffPeriod(serverUrl, sharedSecret, startIso, endIso)

            withContext(Dispatchers.Main) {
                setSaving(false)
                when (result) {
                    is CreateOffPeriodResult.Success -> {
                        // 저장하면 확인 메시지가 뜨고 (done_when)
                        cardValidationWarning.visibility = View.GONE
                        cardConfirmation.visibility = View.VISIBLE
                        textConfirmationMessage.text = getString(R.string.msg_confirm_off_period_created)
                        textConfirmationDetail.text = "쉬는 기간 ID: ${result.id}\n기간: ${result.startAt} ~ ${result.endAt}"

                        showConfirmationDialog(getString(R.string.msg_confirm_off_period_created))
                    }
                    is CreateOffPeriodResult.Failure -> {
                        cardValidationWarning.visibility = View.VISIBLE
                        textValidationMessage.text = result.message
                        cardConfirmation.visibility = View.GONE
                    }
                }
            }
        }
    }

    private fun setSaving(isSaving: Boolean) {
        refreshSaveEnabled(isSaving)
        btnSaveOffPeriod.text = if (isSaving) getString(R.string.btn_saving_off_period) else getString(R.string.btn_save_off_period)
        progressBarSave.visibility = if (isSaving) View.VISIBLE else View.GONE
        btnPickStartDate.isEnabled = !isSaving
        btnPickStartTime.isEnabled = !isSaving
        btnPickEndDate.isEnabled = !isSaving
        btnPickEndTime.isEnabled = !isSaving
        cardStartTab.isEnabled = !isSaving
        cardEndTab.isEnabled = !isSaving
        picker.isEnabled = !isSaving
    }

    /** 종료 > 시작일 때만 저장 가능. 저장 중에도 차단. */
    private fun refreshSaveEnabled(isSaving: Boolean) {
        val ok = !isSaving && RangeCalendarLogic.isValidRange(getStartDateTime(), getEndDateTime())
        btnSaveOffPeriod.isEnabled = ok
        btnSaveOffPeriod.alpha = if (ok || isSaving) 1f else 0.4f
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
