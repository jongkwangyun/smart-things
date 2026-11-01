package com.smartthings.cleaner.model

/**
 * 쉬는 기간 삭제 결과 모델 (REQ-013, LB-022).
 *
 * - must: 삭제 즉시 제한이 해제되어야 한다.
 * - done_when: 진행 중인 쉬는 기간을 삭제하면 목록에서 사라지고, 직후 반복 예약 청소가 시작되어도 정지되지 않는다
 */
sealed class DeleteOffPeriodResult {
    data class Success(val id: String) : DeleteOffPeriodResult()
    data class Failure(val statusCode: Int, val message: String) : DeleteOffPeriodResult()
}
