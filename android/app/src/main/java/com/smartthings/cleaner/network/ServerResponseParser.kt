package com.smartthings.cleaner.network

import com.smartthings.cleaner.model.CreateOffPeriodResult
import org.json.JSONObject

/**
 * 미니PC 서버 JSON 응답 파싱 유틸리티 (LB-064).
 * Android 프레임워크 의존성이 없어 JVM 단위 테스트에서 직접 검증할 수 있다.
 */
object ServerResponseParser {

    data class HealthInfo(
        val serviceName: String,
        val serverTimeKst: String,
        val smartthingsOk: Boolean = true,
        val smartthingsAuthState: String = "unknown",
        val lastSuccessAt: String? = null,
        val lastError: String? = null
    )

    fun optNullableString(json: JSONObject, key: String): String? {
        if (!json.has(key) || json.isNull(key)) return null
        return json.optString(key, "").trim().ifEmpty { null }
    }

    fun parseHealth(body: String): HealthInfo {
        val json = JSONObject(body)
        val smartthingsOk = if (json.has("smartthings_ok") && !json.isNull("smartthings_ok")) {
            json.optBoolean("smartthings_ok", true)
        } else {
            true
        }
        return HealthInfo(
            serviceName = json.optString("service", "smart-things-mini-pc"),
            serverTimeKst = json.optString("time_kst", "-"),
            smartthingsOk = smartthingsOk,
            smartthingsAuthState = optNullableString(json, "smartthings_auth_state") ?: "unknown",
            lastSuccessAt = optNullableString(json, "last_success_at"),
            lastError = optNullableString(json, "last_error")
        )
    }

    fun parseCreateOffPeriodSuccess(
        body: String,
        fallbackStartAt: String,
        fallbackEndAt: String
    ): CreateOffPeriodResult.Success {
        val json = JSONObject(body)
        return CreateOffPeriodResult.Success(
            id = json.optString("id", ""),
            startAt = json.optString("start_at", fallbackStartAt),
            endAt = json.optString("end_at", fallbackEndAt),
            warning = optNullableString(json, "warning")
        )
    }
}
