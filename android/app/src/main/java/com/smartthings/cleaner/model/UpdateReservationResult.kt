package com.smartthings.cleaner.model

/**
 * 1회성 청소 예약 수정 결과 모델 (REQ-003, LB-018).
 *
 * - must: 이미 실행된 예약은 수정할 수 없다.
 * - done_when: 앱에서 예정 예약의 시각을 바꾸면 목록에 새 시각이 보이고, 실제 실행도 새 시각에 일어난다
 */
sealed class UpdateReservationResult {
    data class Success(
        val id: String,
        val scheduledAt: String,
        val status: String,
        val message: String = "예약이 수정되었습니다"
    ) : UpdateReservationResult()

    data class Failure(
        val code: Int,
        val message: String
    ) : UpdateReservationResult()
}
