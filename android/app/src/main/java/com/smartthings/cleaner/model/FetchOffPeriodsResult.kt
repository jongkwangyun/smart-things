package com.smartthings.cleaner.model

/**
 * 쉬는 기간 목록 조회 결과 (GET /api/off-periods).
 *
 * SPEC:
 * - REQ-012: 쉬는 기간 목록 조회
 * - REQ-010: 두 사용자 모두 조회 가능
 * - D-09 / REQ-024: 비밀키 인증 실패 (401)
 * - REQ-025: 서버 연결 불가 처리 (0)
 */
sealed class FetchOffPeriodsResult {
    data class Success(
        val offPeriods: List<OffPeriodItem>
    ) : FetchOffPeriodsResult()

    data class Failure(
        val statusCode: Int,
        val message: String
    ) : FetchOffPeriodsResult()
}
