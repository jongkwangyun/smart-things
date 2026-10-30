package com.smartthings.cleaner.model

/**
 * 푸시 알림 대상 등록 API 응답 모델 (D-12, REQ-022).
 */
sealed class RegisterNotificationResult {
    data class Success(
        val deviceId: String,
        val pushToken: String
    ) : RegisterNotificationResult()

    data class Failure(
        val statusCode: Int,
        val message: String
    ) : RegisterNotificationResult()
}

/**
 * 푸시 알림 발송 API 응답 모델 (D-12, REQ-022).
 */
sealed class SendNotificationResult {
    data class Success(
        val totalTargets: Int,
        val successCount: Int
    ) : SendNotificationResult()

    data class Failure(
        val statusCode: Int,
        val message: String
    ) : SendNotificationResult()
}
