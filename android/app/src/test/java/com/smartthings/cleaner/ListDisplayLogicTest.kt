package com.smartthings.cleaner

import com.smartthings.cleaner.model.OffPeriodItem
import com.smartthings.cleaner.model.ReservationItem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZonedDateTime

class ListDisplayLogicTest {

    private val refNow = ZonedDateTime.parse("2026-11-19T12:00:00+09:00")

    private fun res(id: String, scheduledAt: String, status: String) =
        ReservationItem(id = id, scheduledAt = scheduledAt, status = status)

    private fun off(id: String, startAt: String, endAt: String) =
        OffPeriodItem(id = id, startAt = startAt, endAt = endAt)

    @Test
    fun partitionAndSortReservations_sortsEachSectionDescendingByScheduledAt() {
        val items = listOf(
            res("up-mid", "2026-11-20T09:00:00+09:00", ReservationItem.STATUS_SCHEDULED),
            res("up-early", "2026-11-19T15:00:00+09:00", ReservationItem.STATUS_SCHEDULED),
            res("up-late", "2026-11-22T08:00:00+09:00", ReservationItem.STATUS_SCHEDULED),
            res("past-old", "2026-11-10T09:00:00+09:00", ReservationItem.STATUS_SUCCESS),
            res("past-recent", "2026-11-18T18:00:00+09:00", ReservationItem.STATUS_FAILED),
            res("past-mid", "2026-11-15T10:00:00+09:00", ReservationItem.STATUS_CANCELLED),
            res("past-missed", "2026-11-17T07:00:00+09:00", ReservationItem.STATUS_MISSED)
        )

        val (upcoming, past) = ListDisplayLogic.partitionAndSortReservations(items)

        assertEquals(listOf("up-late", "up-mid", "up-early"), upcoming.map { it.id })
        assertEquals(listOf("past-recent", "past-missed", "past-mid", "past-old"), past.map { it.id })
    }

    @Test
    fun sortReservationsDescending_comparesInstantsAcrossOffsetsAndPutsInvalidLast() {
        val items = listOf(
            res("invalid", "not-a-timestamp", ReservationItem.STATUS_SCHEDULED),
            res("utc-1400-kst", "2026-11-20T05:00:00+00:00", ReservationItem.STATUS_SCHEDULED),
            res("kst-1500", "2026-11-20T15:00:00+09:00", ReservationItem.STATUS_SCHEDULED),
            res("kst-1300", "2026-11-20T13:00:00+09:00", ReservationItem.STATUS_SCHEDULED)
        )

        val sorted = ListDisplayLogic.sortReservationsDescending(items)
        assertEquals(listOf("kst-1500", "utc-1400-kst", "kst-1300", "invalid"), sorted.map { it.id })
    }

    @Test
    fun isPastReservation_distinguishesUpcomingFromPastStatuses() {
        assertFalse(ListDisplayLogic.isPastReservation(res("1", "2026-11-20T09:00:00+09:00", ReservationItem.STATUS_SCHEDULED)))
        assertTrue(ListDisplayLogic.isPastReservation(res("2", "2026-11-18T09:00:00+09:00", ReservationItem.STATUS_SUCCESS)))
        assertTrue(ListDisplayLogic.isPastReservation(res("3", "2026-11-18T09:00:00+09:00", ReservationItem.STATUS_FAILED)))
        assertTrue(ListDisplayLogic.isPastReservation(res("4", "2026-11-18T09:00:00+09:00", ReservationItem.STATUS_CANCELLED)))
        assertTrue(ListDisplayLogic.isPastReservation(res("5", "2026-11-18T09:00:00+09:00", ReservationItem.STATUS_MISSED)))
    }

    @Test
    fun sortOffPeriodsDescending_sortsByStartAtDescending() {
        val items = listOf(
            off("ended", "2026-11-10T09:00:00+09:00", "2026-11-12T18:00:00+09:00"),
            off("future", "2026-11-25T09:00:00+09:00", "2026-11-26T18:00:00+09:00"),
            off("active", "2026-11-19T00:00:00+09:00", "2026-11-20T18:00:00+09:00"),
            off("invalid", "bad-date", "2026-11-21T18:00:00+09:00")
        )

        val sorted = ListDisplayLogic.sortOffPeriodsDescending(items)
        assertEquals(listOf("future", "active", "ended", "invalid"), sorted.map { it.id })
    }

    @Test
    fun isPastOffPeriod_returnsTrueOnlyForEnded() {
        val active = off("active", "2026-11-19T09:00:00+09:00", "2026-11-19T18:00:00+09:00")
        val upcoming = off("upcoming", "2026-11-20T09:00:00+09:00", "2026-11-21T18:00:00+09:00")
        val ended = off("ended", "2026-11-17T09:00:00+09:00", "2026-11-18T18:00:00+09:00")

        assertFalse(ListDisplayLogic.isPastOffPeriod(active, refNow))
        assertFalse(ListDisplayLogic.isPastOffPeriod(upcoming, refNow))
        assertTrue(ListDisplayLogic.isPastOffPeriod(ended, refNow))
    }
}
