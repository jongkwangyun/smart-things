package com.smartthings.cleaner

import com.smartthings.cleaner.model.ReservationItem
import java.time.ZonedDateTime

/**
 * 홈 화면용 순수 계산 로직(LB-047). Android 의존성 없음(JVM 단위 테스트 대상).
 * 서버에는 홈 전용 API가 없으므로(LB-052) 예약 목록(GET /api/reservations)으로 클라이언트에서 계산한다.
 *
 * 건수 정의:
 * - 예정 = status '예정' 이고 시각이 현재(KST) 이후(같은 시각 포함)인 예약.
 * - 지난 = status 가 성공, 실패, 누락, 취소 중 하나인 예약.
 * - 확인 필요 = status '예정' 인데 시각이 이미 지났거나 시각을 해석할 수 없는 예약.
 *   예정 건수에도 지난 건수에도 넣지 않고 따로 센다.
 */
object HomeLogic {

    data class Counts(val upcoming: Int, val past: Int, val needsCheck: Int)

    private fun parse(item: ReservationItem): ZonedDateTime? =
        try { ZonedDateTime.parse(item.scheduledAt) } catch (_: Exception) { null }

    /** 다음 예약 = status '예정' 이고 now 이후인 예약 중 가장 이른 것. 목록 순서에 의존하지 않는다. */
    fun nextReservation(items: List<ReservationItem>, now: ZonedDateTime): ReservationItem? =
        items.asSequence()
            .filter { it.status == ReservationItem.STATUS_SCHEDULED }
            .mapNotNull { item -> parse(item)?.let { item to it } }
            .filter { !it.second.isBefore(now) }
            .minByOrNull { it.second.toInstant() }
            ?.first

    fun counts(items: List<ReservationItem>, now: ZonedDateTime): Counts {
        var upcoming = 0
        var past = 0
        var needsCheck = 0
        for (item in items) {
            if (item.status == ReservationItem.STATUS_SCHEDULED) {
                val at = parse(item)
                if (at != null && !at.isBefore(now)) upcoming++ else needsCheck++
            } else if (item.status in PAST_STATUSES) {
                past++
            }
        }
        return Counts(upcoming, past, needsCheck)
    }

    private val PAST_STATUSES = setOf(
        ReservationItem.STATUS_SUCCESS,
        ReservationItem.STATUS_FAILED,
        ReservationItem.STATUS_MISSED,
        ReservationItem.STATUS_CANCELLED
    )
}
