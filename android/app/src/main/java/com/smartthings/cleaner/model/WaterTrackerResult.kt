package com.smartthings.cleaner.model

/**
 * 물 관리 추적 및 초기화 API 결과 (REQ-019, REQ-020, REQ-021).
 */
sealed class WaterTrackerResult {
    data class Success(val item: WaterTrackerItem) : WaterTrackerResult()
    data class Failure(val statusCode: Int, val message: String) : WaterTrackerResult()
}
