package com.smartthings.cleaner

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import androidx.core.content.ContextCompat
import java.time.LocalDate
import java.time.YearMonth

/**
 * 선택 범위를 셀에 칠해 보여주는 월 달력(LB-050). 안드로이드 CalendarView 는 범위 하이라이트를 지원하지 않아 직접 그린다.
 * 일요일 시작, 한국어 요일. 날짜 계산은 [RangeCalendarLogic] 에 위임한다.
 */
class RangeCalendarView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private val dp = resources.displayMetrics.density
    private val headerH = 44 * dp
    private val weekH = 24 * dp
    private val rowH = 40 * dp
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)

    private var month: YearMonth = YearMonth.now(DateTimePickerLogic.KST)
    private var selected: LocalDate? = null
    private var rangeStart: LocalDate? = null
    private var rangeEnd: LocalDate? = null
    private var rangeValid = true

    /** 선택 가능한 최소 날짜. 이보다 이른 날짜는 비활성. */
    var minDate: LocalDate? = null
        set(v) { field = v; invalidate() }

    var onDateSelected: ((LocalDate) -> Unit)? = null

    private fun color(id: Int) = ContextCompat.getColor(context, id)

    /** 선택 날짜를 지정하고 해당 월로 이동한다(리스너 호출 없음). */
    fun select(date: LocalDate) {
        selected = date
        month = YearMonth.from(date)
        invalidate()
    }

    /** 범위를 칠한다. null 이면 범위 없음. valid=false 이면 오류색(종료 <= 시작). */
    fun setRange(start: LocalDate?, end: LocalDate?, valid: Boolean) {
        rangeStart = start; rangeEnd = end; rangeValid = valid
        invalidate()
    }

    override fun onMeasure(widthSpec: Int, heightSpec: Int) {
        val h = (headerH + weekH + rowH * 6).toInt() + paddingTop + paddingBottom
        setMeasuredDimension(resolveSize(suggestedMinimumWidth, widthSpec), h)
    }

    private fun cellW() = (width - paddingLeft - paddingRight) / 7f

    private fun canGoPrev() = minDate?.let { month.isAfter(YearMonth.from(it)) } ?: true

    override fun onDraw(canvas: Canvas) {
        val w = cellW()
        val left = paddingLeft.toFloat()
        val top = paddingTop.toFloat()
        paint.textAlign = Paint.Align.CENTER
        paint.style = Paint.Style.FILL
        // 헤더: < 2026년 11월 >
        paint.color = color(R.color.text_primary); paint.textSize = 16 * dp; paint.isFakeBoldText = true
        canvas.drawText("${month.year}년 ${month.monthValue}월", width / 2f, top + headerH / 2 + 6 * dp, paint)
        paint.color = if (canGoPrev()) color(R.color.primary) else color(R.color.status_disconnected)
        canvas.drawText("<", left + 24 * dp, top + headerH / 2 + 6 * dp, paint)
        paint.color = color(R.color.primary)
        canvas.drawText(">", width - paddingRight - 24 * dp, top + headerH / 2 + 6 * dp, paint)
        // 요일
        paint.isFakeBoldText = false; paint.textSize = 12 * dp; paint.color = color(R.color.text_secondary)
        for ((i, name) in listOf("일", "월", "화", "수", "목", "금", "토").withIndex()) {
            canvas.drawText(name, left + w * (i + 0.5f), top + headerH + weekH / 2 + 4 * dp, paint)
        }
        // 날짜
        val grid = RangeCalendarLogic.monthGrid(month)
        val accent = if (rangeValid) color(R.color.primary) else color(R.color.status_error)
        val band = if (rangeValid) color(R.color.status_scheduled_bg) else color(R.color.status_error_bg)
        val r = rowH / 2 - 3 * dp
        for ((i, date) in grid.withIndex()) {
            val x = left + w * (i % 7)
            val y = top + headerH + weekH + rowH * (i / 7)
            val cx = x + w / 2
            val cy = y + rowH / 2
            val role = RangeCalendarLogic.roleOf(date, rangeStart, rangeEnd)
            paint.style = Paint.Style.FILL
            paint.color = band
            when (role) {
                RangeCalendarLogic.DayRole.MIDDLE -> canvas.drawRect(x, y + 3 * dp, x + w, y + rowH - 3 * dp, paint)
                RangeCalendarLogic.DayRole.START -> canvas.drawRect(cx, y + 3 * dp, x + w, y + rowH - 3 * dp, paint)
                RangeCalendarLogic.DayRole.END -> canvas.drawRect(x, y + 3 * dp, cx, y + rowH - 3 * dp, paint)
                else -> {}
            }
            val endpoint = role == RangeCalendarLogic.DayRole.START ||
                role == RangeCalendarLogic.DayRole.END || role == RangeCalendarLogic.DayRole.SINGLE
            if (endpoint) {
                paint.color = accent
                canvas.drawCircle(cx, cy, r, paint)
            }
            if (date == selected) {
                paint.style = Paint.Style.STROKE
                paint.strokeWidth = 2 * dp
                paint.color = color(R.color.primary_variant)
                canvas.drawCircle(cx, cy, r + 1 * dp, paint)
                paint.style = Paint.Style.FILL
            }
            val disabled = minDate?.let { date.isBefore(it) } ?: false
            paint.textSize = 14 * dp
            paint.color = when {
                endpoint -> color(R.color.on_primary)
                disabled -> color(R.color.status_disconnected)
                date.monthValue != month.monthValue -> color(R.color.text_secondary)
                else -> color(R.color.text_primary)
            }
            if (disabled && !endpoint) paint.alpha = 70 // 선택 불가(과거) 날짜는 흐리게
            paint.isFakeBoldText = endpoint
            canvas.drawText(date.dayOfMonth.toString(), cx, cy + 5 * dp, paint)
        }
        paint.isFakeBoldText = false
    }

    override fun onTouchEvent(e: MotionEvent): Boolean {
        if (e.action == MotionEvent.ACTION_DOWN) return true
        if (e.action != MotionEvent.ACTION_UP) return false
        val y = e.y - paddingTop
        val x = e.x - paddingLeft
        if (y < 0) return true
        if (y < headerH) {
            if (x < 56 * dp && canGoPrev()) {
                month = month.minusMonths(1); invalidate()
            } else if (x > width - paddingLeft - paddingRight - 56 * dp) {
                month = month.plusMonths(1); invalidate()
            }
            return true
        }
        val row = ((y - headerH - weekH) / rowH).toInt()
        if (y < headerH + weekH || row !in 0..5) return true
        val date = RangeCalendarLogic.monthGrid(month)[row * 7 + (x / cellW()).toInt().coerceIn(0, 6)]
        if (minDate?.let { date.isBefore(it) } == true) return true
        performClick()
        selected = date
        month = YearMonth.from(date)
        invalidate()
        onDateSelected?.invoke(date)
        return true
    }

    override fun performClick(): Boolean = super.performClick()
}
