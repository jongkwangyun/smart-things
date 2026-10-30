package com.smartthings.cleaner.model

/**
 * 쉬는 기간 등록 결과 모델 (REQ-011, LB-020).
 *
 * - done_when: 성공 시 확인 메시지("쉬는 기간이 등록되었습니다")
 */
sealed class CreateOffPeriodResult {
    data class Success(
        val id: String,
        val startAt: String,
        val endAt: String,
        val message: String = "쉬는 기간이 등록되었습니다"
    ) : CreateOffPeriodResult()

    data class Failure(
        val code: Int,
        val message: String
    ) : CreateOffPeriodResult()
}
