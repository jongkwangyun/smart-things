package com.smartthings.cleaner

import android.content.Context
import android.util.AttributeSet
import android.view.LayoutInflater
import android.widget.CalendarView
import android.widget.LinearLayout
import android.widget.NumberPicker
import android.widget.TextView
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZonedDateTime

/**
 * 달력(CalendarView) + 시간 휠(오전/오후·시·분 NumberPicker) + KST 요약을 한 레이아웃에 담은 공용 일시 피커.
 * 날짜/시각 선택 버튼, 다이얼로그, 미리보기 카드를 쓰지 않는다. 모든 일시는 KST.
 *
 * - [value]: 선택값(KST ZonedDateTime). 외부에서 set 하면 달력/휠/요약이 갱신된다(리스너는 호출하지 않음).
 * - [minDateTime]: 선택 가능한 최소 일시(기본: 현재 KST). 이보다 이른 날짜는 달력에서 선택 불가,
 *   같은 날 더 이른 시각은 minDateTime 으로 보정된다.
 * - [onValueChanged]: 사용자가 달력/휠을 바꿨을 때 호출.
 * - [summaryView]: 요약 TextView(외부에서 텍스트를 읽거나 숨길 수 있음).
 * - [rangeCalendarEnabled]: true 이면 CalendarView 대신 선택 범위를 칠하는 [RangeCalendarView] 를 쓴다(LB-050, 기본 false).
 * - [setRange]: 범위 달력 모드에서 시작/종료 범위를 달력 셀에 칠한다.
 * - [setRangeHint]: LB-050 선택 범위 표시용 최소 훅(요약 아래 한 줄 텍스트).
 */
class DateTimeWheelPicker @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : LinearLayout(context, attrs, defStyleAttr) {

    private val calendar: CalendarView
    private val rangeCalendar: RangeCalendarView
    private val amPm: NumberPicker
    private val hour: NumberPicker
    private val minute: NumberPicker
    val summaryView: TextView
    private val rangeHint: TextView

    private var selectedDate: LocalDate
    private var updating = false

    /** 범위 하이라이트 달력 사용 여부. 기본 false(기존 CalendarView 동작 유지). */
    var rangeCalendarEnabled: Boolean = false
        set(v) {
            field = v
            calendar.visibility = if (v) GONE else VISIBLE
            rangeCalendar.visibility = if (v) VISIBLE else GONE
        }

    var onValueChanged: ((ZonedDateTime) -> Unit)? = null

    var minDateTime: ZonedDateTime = DateTimePickerLogic.nowKst()
        set(v) {
            field = DateTimePickerLogic.toKst(v)
            applyMinDate()
            value = value // 재보정
        }

    var value: ZonedDateTime
        get() = DateTimePickerLogic.compose(
            selectedDate, amPm.value == 1, hour.value, minute.value
        )
        set(v) {
            val clamped = DateTimePickerLogic.clampToMin(v, minDateTime)
            updating = true
            selectedDate = clamped.toLocalDate()
            calendar.date = epochMillisOfDate(selectedDate)
            rangeCalendar.select(selectedDate)
            val (pm, h12) = DateTimePickerLogic.from24Hour(clamped.hour)
            amPm.value = if (pm) 1 else 0
            hour.value = h12
            minute.value = clamped.minute
            updating = false
            refreshSummary()
        }

    init {
        orientation = VERTICAL
        LayoutInflater.from(context).inflate(R.layout.view_datetime_picker, this)
        calendar = findViewById(R.id.dtpCalendar)
        rangeCalendar = findViewById(R.id.dtpRangeCalendar)
        amPm = findViewById(R.id.dtpAmPm)
        hour = findViewById(R.id.dtpHour)
        minute = findViewById(R.id.dtpMinute)
        summaryView = findViewById(R.id.dtpSummary)
        rangeHint = findViewById(R.id.dtpRangeHint)

        amPm.minValue = 0; amPm.maxValue = 1
        amPm.displayedValues = arrayOf("오전", "오후")
        hour.minValue = 1; hour.maxValue = 12
        hour.setFormatter { String.format("%d", it) }
        minute.minValue = 0; minute.maxValue = 59
        minute.setFormatter { String.format("%02d", it) }
        for (p in listOf(amPm, hour, minute)) p.wrapSelectorWheel = p !== amPm

        selectedDate = minDateTime.toLocalDate()
        applyMinDate()
        value = minDateTime

        calendar.setOnDateChangeListener { _, y, m, d ->
            if (updating) return@setOnDateChangeListener
            selectedDate = LocalDate.of(y, m + 1, d)
            onUserChanged()
        }
        rangeCalendar.onDateSelected = { date ->
            if (!updating) {
                selectedDate = date
                onUserChanged()
            }
        }
        val wheelListener = NumberPicker.OnValueChangeListener { _, _, _ ->
            if (!updating) onUserChanged()
        }
        amPm.setOnValueChangedListener(wheelListener)
        hour.setOnValueChangedListener(wheelListener)
        minute.setOnValueChangedListener(wheelListener)
    }

    /** 사용자 변경: 과거로 내려가면 minDateTime 으로 보정 후 요약 갱신 + 리스너 통지. */
    private fun onUserChanged() {
        val current = value
        if (DateTimePickerLogic.isPast(current, minDateTime)) {
            value = minDateTime
        } else {
            refreshSummary()
        }
        onValueChanged?.invoke(value)
    }

    private fun refreshSummary() {
        summaryView.text = DateTimePickerLogic.summary(value)
    }

    private fun applyMinDate() {
        // CalendarView 는 기기 기본 시간대 기준으로 날짜를 해석하므로 KST 날짜의 자정을 그 시간대로 환산한다.
        calendar.minDate = epochMillisOfDate(minDateTime.toLocalDate())
        rangeCalendar.minDate = minDateTime.toLocalDate()
    }

    private fun epochMillisOfDate(date: LocalDate): Long =
        date.atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli()

    /** 선택 범위 표시 최소 훅(LB-050). 둘 다 null 이면 숨김. */
    fun setRangeHint(start: ZonedDateTime?, end: ZonedDateTime?) {
        if (start == null || end == null) {
            rangeHint.visibility = GONE
            return
        }
        rangeHint.text = "${DateTimePickerLogic.summary(start)} ~ ${DateTimePickerLogic.summary(end)}"
        rangeHint.visibility = VISIBLE
    }

    /** 범위 달력 모드: 시작~종료 날짜를 달력 셀에 칠한다. 종료 <= 시작이면 오류색. null 이면 지운다. */
    fun setRange(start: ZonedDateTime?, end: ZonedDateTime?) {
        if (start == null || end == null) {
            rangeCalendar.setRange(null, null, true)
            return
        }
        val s = DateTimePickerLogic.toKst(start)
        val e = DateTimePickerLogic.toKst(end)
        rangeCalendar.setRange(s.toLocalDate(), e.toLocalDate(), RangeCalendarLogic.isValidRange(s, e))
    }
}
