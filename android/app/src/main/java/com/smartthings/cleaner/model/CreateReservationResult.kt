package com.smartthings.cleaner.model

/**
 * 1회성 청소 예약 생성 결과 모델 (REQ-001, LB-013).
 *
 * - D-03: 모드·구역 선택 없이 기본 전체 청소로 등록
 * - done_when: 성공 시 확인 메시지("예약이 등록되었습니다")
 */
sealed class CreateReservationResult {
    data class Success(
        val id: String,
        val scheduledAt: String,
        val status: String,
        val message: String = "예약이 등록되었습니다"
    ) : CreateReservationResult()

    data class Failure(
        val code: Int,
        val message: String
    ) : CreateReservationResult()
}
