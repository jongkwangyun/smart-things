package com.smartthings.cleaner

import java.time.Duration
import java.time.LocalDate
import java.time.YearMonth
import java.time.ZonedDateTime

/**
 * 쉬는 기간 범위 달력/요약의 순수 로직(LB-050). Android 의존성 없음(JVM 단위 테스트 대상). 모든 일시는 KST.
 */
object RangeCalendarLogic {
    /** 달력 셀이 범위에서 맡는 역할. */
    enum class DayRole { NONE, START, MIDDLE, END, SINGLE }

    const val GRID_CELLS = 42 // 6주 x 7일 (일요일 시작)

    /** 종료가 시작보다 뒤이면 true. 같거나 앞서면 false(등록 불가, 즉시 경고 대상). */
    fun isValidRange(start: ZonedDateTime, end: ZonedDateTime): Boolean =
        DateTimePickerLogic.toKst(end).isAfter(DateTimePickerLogic.toKst(start))

    /** 해당 월을 표시하는 42칸 격자(일요일 시작). 이전/다음 달 날짜가 앞뒤를 채운다. */
    fun monthGrid(month: YearMonth): List<LocalDate> {
        val first = month.atDay(1)
        val offset = first.dayOfWeek.value % 7 // 일=0 ... 토=6
        val gridStart = first.minusDays(offset.toLong())
        return List(GRID_CELLS) { gridStart.plusDays(it.toLong()) }
    }

    /**
     * 날짜 셀의 범위 역할. 종료 날짜가 시작 날짜보다 앞서는 잘못된 범위는 칠하지 않고
     * 양 끝 날짜만 SINGLE 로 표시한다(뷰가 오류색으로 그림).
     */
    fun roleOf(date: LocalDate, start: LocalDate?, end: LocalDate?): DayRole {
        if (start == null || end == null) return DayRole.NONE
        if (end.isBefore(start)) {
            return if (date == start || date == end) DayRole.SINGLE else DayRole.NONE
        }
        if (start == end) return if (date == start) DayRole.SINGLE else DayRole.NONE
        return when {
            date == start -> DayRole.START
            date == end -> DayRole.END
            date.isAfter(start) && date.isBefore(end) -> DayRole.MIDDLE
            else -> DayRole.NONE
        }
    }

    /** 기간 길이 문구. 예: "4일 9시간". 잘못된 범위이면 빈 문자열. */
    fun durationText(start: ZonedDateTime, end: ZonedDateTime): String {
        if (!isValidRange(start, end)) return ""
        val d = Duration.between(DateTimePickerLogic.toKst(start), DateTimePickerLogic.toKst(end))
        val parts = ArrayList<String>()
        if (d.toDays() > 0) parts.add("${d.toDays()}일")
        if (d.toHoursPart() > 0) parts.add("${d.toHoursPart()}시간")
        if (d.toMinutesPart() > 0) parts.add("${d.toMinutesPart()}분")
        return parts.joinToString(" ")
    }
}
