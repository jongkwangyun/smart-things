package com.smartthings.cleaner

import com.smartthings.cleaner.model.ReservationItem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.ZonedDateTime

class HomeLogicTest {
    private val now = ZonedDateTime.parse("2026-11-05T12:00:00+09:00")

    private fun r(id: String, at: String, status: String) = ReservationItem(id, at, status)

    @Test fun next_isEarliestFutureScheduled_notFirstInList() {
        val items = listOf(
            r("old", "2026-11-04T09:00:00+09:00", "예정"),
            r("done", "2026-11-05T13:00:00+09:00", "성공"),
            r("late", "2026-11-07T09:00:00+09:00", "예정"),
            r("soon", "2026-11-05T15:00:00+09:00", "예정")
        )
        assertEquals("soon", HomeLogic.nextReservation(items, now)?.id)
    }

    @Test fun next_comparesInstantsAcrossOffsets() {
        val items = listOf(
            r("a", "2026-11-05T05:00:00+00:00", "예정"), // 14:00 KST
            r("b", "2026-11-05T13:30:00+09:00", "예정")  // 13:30 KST, earlier
        )
        assertEquals("b", HomeLogic.nextReservation(items, now)?.id)
    }

    @Test fun next_nullWhenNoneUpcoming() {
        val items = listOf(r("p", "2026-11-04T09:00:00+09:00", "예정"), r("x", "2026-11-06T09:00:00+09:00", "취소"))
        assertNull(HomeLogic.nextReservation(items, now))
        assertNull(HomeLogic.nextReservation(emptyList(), now))
    }

    @Test fun next_skipsUnparsable() {
        assertNull(HomeLogic.nextReservation(listOf(r("bad", "not-a-date", "예정")), now))
    }

    @Test fun next_exactlyNowIsIncluded() {
        assertEquals("n", HomeLogic.nextReservation(listOf(r("n", "2026-11-05T12:00:00+09:00", "예정")), now)?.id)
    }

    @Test fun counts_separatesUpcomingPastAndNeedsCheck() {
        val items = listOf(
            r("1", "2026-11-06T09:00:00+09:00", "예정"),
            r("2", "2026-11-07T09:00:00+09:00", "예정"),
            r("3", "2026-11-04T09:00:00+09:00", "예정"), // 시각 경과
            r("4", "bad", "예정"),
            r("5", "2026-11-03T09:00:00+09:00", "성공"),
            r("6", "2026-11-02T09:00:00+09:00", "실패"),
            r("7", "2026-11-01T09:00:00+09:00", "누락"),
            r("8", "2026-11-08T09:00:00+09:00", "취소")
        )
        assertEquals(HomeLogic.Counts(upcoming = 2, past = 4, needsCheck = 2), HomeLogic.counts(items, now))
    }

    @Test fun counts_empty() {
        assertEquals(HomeLogic.Counts(0, 0, 0), HomeLogic.counts(emptyList(), now))
    }
}
