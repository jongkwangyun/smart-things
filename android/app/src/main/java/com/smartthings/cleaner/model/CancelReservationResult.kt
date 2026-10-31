package com.smartthings.cleaner.model

/**
 * 1회성 청소 예약 취소 결과 모델 (REQ-004, LB-019).
 *
 * - must: 이미 실행된 예약은 취소할 수 없다.
 * - done_when: 앱에서 예정 예약을 취소하면 목록에 '취소'로 표시되고, 그 시각이 지나도 청소기가 출발하지 않는다
 */
sealed class CancelReservationResult {
    data class Success(
        val id: String,
        val status: String,
        val message: String = "예약이 취소되었습니다"
    ) : CancelReservationResult()

    data class Failure(
        val code: Int,
        val message: String
    ) : CancelReservationResult()
}
