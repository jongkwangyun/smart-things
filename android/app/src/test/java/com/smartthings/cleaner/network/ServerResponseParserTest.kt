package com.smartthings.cleaner.network

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerResponseParserTest {

    @Test
    fun parseHealth_withSmartThingsFailure_extractsAllFields() {
        val json = """
            {
              "status": "ok",
              "service": "smart-things-mini-pc",
              "time_kst": "2026-11-19T12:00:00+09:00",
              "database": "connected",
              "smartthings_ok": false,
              "smartthings_auth_state": "auth_failed",
              "last_success_at": "2026-11-19T10:00:00+09:00",
              "last_error": "HTTP 401 invalid token"
            }
        """.trimIndent()
        val info = ServerResponseParser.parseHealth(json)
        assertEquals("smart-things-mini-pc", info.serviceName)
        assertEquals("2026-11-19T12:00:00+09:00", info.serverTimeKst)
        assertFalse(info.smartthingsOk)
        assertEquals("auth_failed", info.smartthingsAuthState)
        assertEquals("2026-11-19T10:00:00+09:00", info.lastSuccessAt)
        assertEquals("HTTP 401 invalid token", info.lastError)
    }

    @Test
    fun parseHealth_withNullStrings_returnsNullNotLiteralNull() {
        val json = """
            {
              "status": "ok",
              "service": "smart-things-mini-pc",
              "time_kst": "2026-11-19T12:00:00+09:00",
              "smartthings_ok": true,
              "smartthings_auth_state": "unknown",
              "last_success_at": null,
              "last_error": null
            }
        """.trimIndent()
        val info = ServerResponseParser.parseHealth(json)
        assertTrue(info.smartthingsOk)
        assertEquals("unknown", info.smartthingsAuthState)
        assertNull(info.lastSuccessAt)
        assertNull(info.lastError)
    }

    @Test
    fun parseHealth_legacyPayloadWithoutSmartThingsFields_defaultsSafe() {
        val json = """{"status": "ok", "service": "smart-things-mini-pc", "time_kst": "2026-11-19T12:00:00+09:00"}"""
        val info = ServerResponseParser.parseHealth(json)
        assertTrue(info.smartthingsOk)
        assertEquals("unknown", info.smartthingsAuthState)
        assertNull(info.lastSuccessAt)
        assertNull(info.lastError)
    }

    @Test
    fun parseCreateOffPeriodSuccess_withWarning_extractsWarning() {
        val json = """
            {
              "id": "off-1234",
              "start_at": "2026-11-20T09:00:00+09:00",
              "end_at": "2026-11-21T18:00:00+09:00",
              "warning": "쉬는 기간은 저장되었으나 스마트싱스 인증 실패로 DND 동기화를 완료하지 못했습니다"
            }
        """.trimIndent()
        val res = ServerResponseParser.parseCreateOffPeriodSuccess(json, "", "")
        assertEquals("off-1234", res.id)
        assertEquals("쉬는 기간은 저장되었으나 스마트싱스 인증 실패로 DND 동기화를 완료하지 못했습니다", res.warning)
    }

    @Test
    fun parseCreateOffPeriodSuccess_withoutOrNullWarning_returnsNullWarning() {
        val noWarn = """{"id": "off-1", "start_at": "2026-11-20T09:00:00+09:00", "end_at": "2026-11-21T18:00:00+09:00"}"""
        assertNull(ServerResponseParser.parseCreateOffPeriodSuccess(noWarn, "", "").warning)

        val nullWarn = """{"id": "off-2", "start_at": "s", "end_at": "e", "warning": null}"""
        assertNull(ServerResponseParser.parseCreateOffPeriodSuccess(nullWarn, "", "").warning)

        val blankWarn = """{"id": "off-3", "start_at": "s", "end_at": "e", "warning": "   "}"""
        assertNull(ServerResponseParser.parseCreateOffPeriodSuccess(blankWarn, "", "").warning)
    }
}
