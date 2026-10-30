package com.smartthings.cleaner.model

import org.json.JSONObject
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * 1회성 청소 예약 데이터 모델.
 *
 * SPEC:
 * - REQ-002: 예약 목록 조회 (예정된 예약과 지난 예약, 일시와 상태)
 * - REQ-009: 실패 이유 및 마지막 기기 상태값 표시
 * - D-03: 모드·구역 선택 없음 (기본 전체 청소)
 * - F-2: 예약 관리 (목록 조회)
 * - must: 다섯 가지 상태(예정·성공·실패·취소·누락)를 모두 구분해 표시한다.
 */
data class ReservationItem(
    val id: String,
    val scheduledAt: String,
    val status: String,
    val failureReason: String? = null,
    val lastDeviceState: String? = null,
    val allowDuringOffPeriod: Boolean = false,
    val createdAt: String = "",
    val updatedAt: String = ""
) {
    companion object {
        const val STATUS_SCHEDULED = "예정"
        const val STATUS_SUCCESS = "성공"
        const val STATUS_FAILED = "실패"
        const val STATUS_CANCELLED = "취소"
        const val STATUS_MISSED = "누락"

        val ALL_STATUSES = listOf(
            STATUS_SCHEDULED,
            STATUS_SUCCESS,
            STATUS_FAILED,
            STATUS_CANCELLED,
            STATUS_MISSED
        )

        private val DISPLAY_FORMATTER =
            DateTimeFormatter.ofPattern("yyyy년 MM월 dd일 HH:mm (KST)", Locale.KOREAN)

        fun fromJson(json: JSONObject): ReservationItem {
            val id = json.optString("id", "")
            val scheduledAt = json.optString(
                "scheduled_at",
                json.optString("reserved_at", "")
            )
            val status = json.optString("status", STATUS_SCHEDULED)
            val failureReason = if (json.has("failure_reason") && !json.isNull("failure_reason")) {
                json.optString("failure_reason")
            } else {
                null
            }
            val lastDeviceState = if (json.has("last_device_state") && !json.isNull("last_device_state")) {
                json.optString("last_device_state")
            } else {
                null
            }
            val allowDuringOff = json.optBoolean("allow_during_off_period", false)
            val createdAt = json.optString("created_at", "")
            val updatedAt = json.optString("updated_at", "")

            return ReservationItem(
                id = id,
                scheduledAt = scheduledAt,
                status = status,
                failureReason = failureReason,
                lastDeviceState = lastDeviceState,
                allowDuringOffPeriod = allowDuringOff,
                createdAt = createdAt,
                updatedAt = updatedAt
            )
        }
    }

    /**
     * 예정된 예약 여부 판정 (REQ-002).
     * 상태가 '예정'인 경우 예정 목록에, 나머지(성공·실패·취소·누락)는 지난 예약 목록에 표시.
     */
    fun isUpcoming(): Boolean = status == STATUS_SCHEDULED

    /**
     * KST 기준 사용자 친화적 일시 문자열 반환.
     * 파싱 실패 시 원본 문자열 반환.
     */
    fun formatKstDisplay(): String {
        return try {
            val parsed = ZonedDateTime.parse(scheduledAt)
            parsed.format(DISPLAY_FORMATTER)
        } catch (e: Exception) {
            // ISO 형식이 아닌 경우 원본 반환
            scheduledAt
        }
    }
}
