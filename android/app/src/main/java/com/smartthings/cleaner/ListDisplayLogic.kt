package com.smartthings.cleaner

import com.smartthings.cleaner.model.OffPeriodItem
import com.smartthings.cleaner.model.ReservationItem
import java.time.Instant
import java.time.ZonedDateTime

/**
 * 예약 목록·쉬는 기간 목록 화면의 정렬 및 지난 항목 판정 순수 로직(LB-067).
 * Android 의존성 없이 JVM 단위 테스트가 가능하도록 분리한다.
 */
object ListDisplayLogic {

    private fun parseInstantOrNull(iso: String): Instant? =
        try {
            ZonedDateTime.parse(iso).toInstant()
        } catch (_: Exception) {
            null
        }

    /** 예약 목록을 시작 시각(scheduledAt) 내림차순으로 정렬한다. */
    fun sortReservationsDescending(items: List<ReservationItem>): List<ReservationItem> =
        items.sortedWith(
            compareByDescending<ReservationItem> { parseInstantOrNull(it.scheduledAt) ?: Instant.MIN }
                .thenByDescending { it.scheduledAt }
                .thenByDescending { it.id }
        )

    /** 예약을 예정·지난 목록으로 분리하고 각 목록을 시작 시각 내림차순으로 정렬한다. */
    fun partitionAndSortReservations(
        items: List<ReservationItem>
    ): Pair<List<ReservationItem>, List<ReservationItem>> = Pair(
        sortReservationsDescending(items.filter { it.isUpcoming() }),
        sortReservationsDescending(items.filter { !it.isUpcoming() })
    )

    /** 지난 예약 여부(회색 카드 배경 대상)를 판정한다. */
    fun isPastReservation(item: ReservationItem): Boolean = !item.isUpcoming()

    /** 쉬는 기간 목록을 시작 시각(startAt) 내림차순으로 정렬한다. */
    fun sortOffPeriodsDescending(items: List<OffPeriodItem>): List<OffPeriodItem> =
        items.sortedWith(
            compareByDescending<OffPeriodItem> { parseInstantOrNull(it.startAt) ?: Instant.MIN }
                .thenByDescending { parseInstantOrNull(it.endAt) ?: Instant.MIN }
                .thenByDescending { it.id }
        )

    /** 종료된 쉬는 기간 여부(회색 카드 배경 대상)를 판정한다. */
    fun isPastOffPeriod(
        item: OffPeriodItem,
        referenceTime: ZonedDateTime = ZonedDateTime.now(OffPeriodItem.KST_ZONE)
    ): Boolean = item.getStatus(referenceTime) == OffPeriodItem.STATUS_ENDED
}
