package com.smartthings.cleaner.model

import org.json.JSONObject
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * 쉬는 기간 데이터 모델 (REQ-012, REQ-010, D-09).
 *
 * SPEC:
 * - REQ-012: 쉬는 기간 조회 (등록된 쉬는 기간 목록 표시)
 * - done_when: 앱의 쉬는 기간 화면에 등록된 기간들이 시작·종료 일시와 함께 보인다
 * - must: 두 사용자가 등록한 기간이 모두 보인다.
 */
data class OffPeriodItem(
    val id: String,
    val startAt: String,
    val endAt: String,
    val createdAt: String = ""
) {
    companion object {
        const val STATUS_ACTIVE = "진행 중"
        const val STATUS_UPCOMING = "예정"
        const val STATUS_ENDED = "종료"

        val KST_ZONE: ZoneId = ZoneId.of("Asia/Seoul")

        private val DISPLAY_FORMATTER =
            DateTimeFormatter.ofPattern("yyyy년 MM월 dd일 HH:mm (KST)", Locale.KOREAN)

        fun isDateTimeInOffPeriods(dateTime: ZonedDateTime, offPeriods: List<OffPeriodItem>): Boolean =
            offPeriods.any { it.contains(dateTime) }

        fun fromJson(json: JSONObject): OffPeriodItem {
            return OffPeriodItem(
                id = json.optString("id", ""),
                startAt = json.optString("start_at", ""),
                endAt = json.optString("end_at", ""),
                createdAt = json.optString("created_at", "")
            )
        }
    }

    fun contains(dateTime: ZonedDateTime): Boolean {
        val start = parseStartAt() ?: return false
        val end = parseEndAt() ?: return false
        return !dateTime.isBefore(start) && !dateTime.isAfter(end)
    }

    fun parseStartAt(): ZonedDateTime? {
        return try {
            ZonedDateTime.parse(startAt)
        } catch (_: Exception) {
            null
        }
    }

    fun parseEndAt(): ZonedDateTime? {
        return try {
            ZonedDateTime.parse(endAt)
        } catch (_: Exception) {
            null
        }
    }

    fun formatStartKst(): String {
        val parsed = parseStartAt() ?: return startAt
        return parsed.format(DISPLAY_FORMATTER)
    }

    fun formatEndKst(): String {
        val parsed = parseEndAt() ?: return endAt
        return parsed.format(DISPLAY_FORMATTER)
    }

    fun formatRangeKst(): String {
        return "${formatStartKst()} ~ ${formatEndKst()}"
    }

    /**
     * 현재 시각 기준 쉬는 기간의 상태 판정 (진행 중 / 예정 / 종료).
     */
    fun getStatus(referenceTime: ZonedDateTime = ZonedDateTime.now(KST_ZONE)): String {
        val startZdt = parseStartAt()
        val endZdt = parseEndAt()
        if (startZdt == null || endZdt == null) return STATUS_UPCOMING

        return when {
            referenceTime.isBefore(startZdt) -> STATUS_UPCOMING
            referenceTime.isAfter(endZdt) -> STATUS_ENDED
            else -> STATUS_ACTIVE
        }
    }
}
