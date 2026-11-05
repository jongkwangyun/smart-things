package com.smartthings.cleaner

import com.smartthings.cleaner.RangeCalendarLogic.DayRole
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate
import java.time.YearMonth
import java.time.ZonedDateTime

class RangeCalendarLogicTest {
    private val kst = DateTimePickerLogic.KST
    private fun z(d: Int, h: Int, m: Int = 0) = ZonedDateTime.of(2026, 11, d, h, m, 0, 0, kst)
    private fun d(day: Int) = LocalDate.of(2026, 11, day)

    @Test fun isValidRange_endMustBeAfterStart() {
        assertTrue(RangeCalendarLogic.isValidRange(z(10, 9), z(14, 18)))
        assertTrue(RangeCalendarLogic.isValidRange(z(10, 9), z(10, 9, 1)))
        assertFalse(RangeCalendarLogic.isValidRange(z(10, 9), z(10, 9)))
        assertFalse(RangeCalendarLogic.isValidRange(z(10, 9), z(10, 8)))
        assertFalse(RangeCalendarLogic.isValidRange(z(10, 9), z(9, 18)))
    }

    @Test fun monthGrid_sundayFirst_42cells() {
        val g = RangeCalendarLogic.monthGrid(YearMonth.of(2026, 11)) // 2026-11-01 은 일요일
        assertEquals(42, g.size)
        assertEquals(LocalDate.of(2026, 11, 1), g[0])
        val g2 = RangeCalendarLogic.monthGrid(YearMonth.of(2026, 12)) // 12-01 은 화요일
        assertEquals(LocalDate.of(2026, 11, 29), g2[0])
        assertEquals(LocalDate.of(2026, 12, 1), g2[2])
    }

    @Test fun roleOf_validRange() {
        assertEquals(DayRole.NONE, RangeCalendarLogic.roleOf(d(9), d(10), d(14)))
        assertEquals(DayRole.START, RangeCalendarLogic.roleOf(d(10), d(10), d(14)))
        assertEquals(DayRole.MIDDLE, RangeCalendarLogic.roleOf(d(12), d(10), d(14)))
        assertEquals(DayRole.END, RangeCalendarLogic.roleOf(d(14), d(10), d(14)))
        assertEquals(DayRole.NONE, RangeCalendarLogic.roleOf(d(15), d(10), d(14)))
    }

    @Test fun roleOf_sameDayAndInvertedAndNull() {
        assertEquals(DayRole.SINGLE, RangeCalendarLogic.roleOf(d(10), d(10), d(10)))
        assertEquals(DayRole.NONE, RangeCalendarLogic.roleOf(d(11), d(10), d(10)))
        // 종료 날짜가 시작 날짜보다 앞서면 칠하지 않고 양 끝만 표시
        assertEquals(DayRole.SINGLE, RangeCalendarLogic.roleOf(d(10), d(10), d(8)))
        assertEquals(DayRole.SINGLE, RangeCalendarLogic.roleOf(d(8), d(10), d(8)))
        assertEquals(DayRole.NONE, RangeCalendarLogic.roleOf(d(9), d(10), d(8)))
        assertEquals(DayRole.NONE, RangeCalendarLogic.roleOf(d(10), null, d(8)))
    }

    @Test fun durationText() {
        assertEquals("4일 9시간", RangeCalendarLogic.durationText(z(10, 9), z(14, 18)))
        assertEquals("30분", RangeCalendarLogic.durationText(z(10, 9), z(10, 9, 30)))
        assertEquals("1일", RangeCalendarLogic.durationText(z(10, 9), z(11, 9)))
        assertEquals("", RangeCalendarLogic.durationText(z(10, 9), z(10, 9)))
        assertEquals("", RangeCalendarLogic.durationText(z(10, 9), z(9, 9)))
    }
}
