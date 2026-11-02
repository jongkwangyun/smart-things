package com.smartthings.cleaner.model

import org.json.JSONObject
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * 물걸레 청소 횟수 및 물 관리 상태 아이템 (REQ-019, REQ-020, REQ-021, D-14).
 */
data class WaterTrackerItem(
    val mopCleanCount: Int,
    val lastRefilledAt: String?,
    val alertThresholdN: Int,
    val updatedAt: String = ""
) {
    companion object {
        private val DISPLAY_FORMATTER =
            DateTimeFormatter.ofPattern("yyyy년 MM월 dd일 HH:mm ('KST')", Locale.KOREAN)

        fun fromJson(json: JSONObject): WaterTrackerItem {
            val count = json.optInt("mop_clean_count", 0)
            val refilledAt = if (json.has("last_refilled_at") && !json.isNull("last_refilled_at")) {
                val s = json.optString("last_refilled_at", "")
                if (s.isEmpty()) null else s
            } else null
            val threshold = json.optInt("alert_threshold_n", 7)
            val updated = json.optString("updated_at", "")
            return WaterTrackerItem(count, refilledAt, threshold, updated)
        }
    }

    fun formatLastRefilledDisplay(): String {
        val raw = lastRefilledAt ?: return "기록 없음"
        return try {
            ZonedDateTime.parse(raw).format(DISPLAY_FORMATTER)
        } catch (_: Exception) {
            raw
        }
    }
}
