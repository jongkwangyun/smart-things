package com.smartthings.cleaner.model

/**
 * 예약 목록 조회 결과 (GET /api/reservations).
 *
 * SPEC:
 * - REQ-002: 예약 목록 조회
 * - REQ-024: 비밀키 인증 실패 (401)
 * - REQ-025: 서버 연결 불가 처리 (0)
 */
sealed class FetchReservationsResult {
    data class Success(
        val reservations: List<ReservationItem>
    ) : FetchReservationsResult()

    data class Failure(
        val statusCode: Int,
        val message: String
    ) : FetchReservationsResult()
}
