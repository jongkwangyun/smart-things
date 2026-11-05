package com.smartthings.cleaner

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZonedDateTime

class DateTimePickerLogicTest {
    private val kst = DateTimePickerLogic.KST

    @Test fun to24Hour_boundaries() {
        assertEquals(0, DateTimePickerLogic.to24Hour(false, 12))
        assertEquals(1, DateTimePickerLogic.to24Hour(false, 1))
        assertEquals(11, DateTimePickerLogic.to24Hour(false, 11))
        assertEquals(12, DateTimePickerLogic.to24Hour(true, 12))
        assertEquals(13, DateTimePickerLogic.to24Hour(true, 1))
        assertEquals(23, DateTimePickerLogic.to24Hour(true, 11))
    }

    @Test fun from24Hour_roundTrip() {
        for (h in 0..23) {
            val (pm, h12) = DateTimePickerLogic.from24Hour(h)
            assertEquals(h, DateTimePickerLogic.to24Hour(pm, h12))
        }
        assertEquals(Pair(false, 12), DateTimePickerLogic.from24Hour(0))
        assertEquals(Pair(true, 12), DateTimePickerLogic.from24Hour(12))
    }

    @Test fun compose_isKst() {
        val z = DateTimePickerLogic.compose(LocalDate.of(2026, 11, 6), true, 3, 7)
        assertEquals(kst, z.zone)
        assertEquals(15, z.hour)
        assertEquals(7, z.minute)
        assertEquals("+09:00", z.offset.id)
    }

    @Test fun toKst_convertsOtherZone() {
        val utc = ZonedDateTime.of(2026, 11, 5, 0, 30, 0, 0, ZoneId.of("UTC"))
        val k = DateTimePickerLogic.toKst(utc)
        assertEquals(9, k.hour)
        assertEquals(30, k.minute)
    }

    @Test fun isPast_and_clamp() {
        val min = ZonedDateTime.of(2026, 11, 5, 10, 0, 0, 0, kst)
        val before = min.minusMinutes(1)
        assertTrue(DateTimePickerLogic.isPast(before, min))
        assertFalse(DateTimePickerLogic.isPast(min, min))
        assertFalse(DateTimePickerLogic.isPast(min.plusMinutes(1), min))
        assertEquals(min, DateTimePickerLogic.clampToMin(before, min))
        assertEquals(min.plusDays(1), DateTimePickerLogic.clampToMin(min.plusDays(1), min))
    }

    @Test fun summary_format() {
        val z = ZonedDateTime.of(2026, 11, 5, 15, 7, 0, 0, kst)
        assertEquals("2026-11-05 (목) 오후 3:07 KST", DateTimePickerLogic.summary(z))
    }
}
