package com.smartthings.cleaner

import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import com.smartthings.cleaner.network.TailscaleDiagnoser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class HomeWarningLogicTest {

    @Test
    fun resolve_connectedAndSmartThingsOk_returnsNone() {
        val state = ConnectionState.Connected(
            serverUrl = "http://100.85.1.2:8080",
            serviceName = "smart-things-mini-pc",
            serverTimeKst = "2026-11-19T12:00:00+09:00",
            reservationCount = 2,
            smartthingsOk = true,
            smartthingsAuthState = "ok"
        )
        val warning = HomeWarningLogic.resolve(state)
        assertEquals(HomeWarningLogic.Kind.NONE, warning.kind)
        assertFalse(warning.isVisible)
        assertFalse(warning.isServerConnectionFailure)
        assertFalse(warning.isSmartThingsError)
    }

    @Test
    fun resolve_connectedAndSmartThingsAuthFailed_returnsAuthWarning() {
        val state = ConnectionState.Connected(
            serverUrl = "http://100.85.1.2:8080",
            serviceName = "smart-things-mini-pc",
            serverTimeKst = "2026-11-19T12:00:00+09:00",
            reservationCount = 1,
            smartthingsOk = false,
            smartthingsAuthState = "auth_failed",
            smartthingsLastSuccessAt = "2026-11-19T10:00:00+09:00",
            smartthingsLastError = "HTTP 401 unauthorized"
        )
        val warning = HomeWarningLogic.resolve(state)
        assertEquals(HomeWarningLogic.Kind.SMARTTHINGS_AUTH_FAILED, warning.kind)
        assertTrue(warning.isVisible)
        assertTrue(warning.isSmartThingsError)
        assertFalse(warning.isServerConnectionFailure)
        assertEquals("HTTP 401 unauthorized", warning.detail)
        assertEquals("2026-11-19T10:00:00+09:00", warning.lastSuccessAt)
    }

    @Test
    fun resolve_connectedAndSmartThingsApiError_returnsApiWarning() {
        val state = ConnectionState.Connected(
            serverUrl = "http://100.85.1.2:8080",
            serviceName = "smart-things-mini-pc",
            serverTimeKst = "2026-11-19T12:00:00+09:00",
            reservationCount = 0,
            smartthingsOk = false,
            smartthingsAuthState = "api_error",
            smartthingsLastSuccessAt = null,
            smartthingsLastError = "timeout-503"
        )
        val warning = HomeWarningLogic.resolve(state)
        assertEquals(HomeWarningLogic.Kind.SMARTTHINGS_API_ERROR, warning.kind)
        assertTrue(warning.isVisible)
        assertTrue(warning.isSmartThingsError)
        assertFalse(warning.isServerConnectionFailure)
        assertEquals("timeout-503", warning.detail)
        assertNull(warning.lastSuccessAt)
    }

    @Test
    fun resolve_serverUnreachable_distinguishedFromSmartThingsError() {
        val state = ConnectionState.Failed(
            error = ConnectionError.UNREACHABLE,
            message = "서버에 연결할 수 없습니다: Connection refused"
        )
        val warning = HomeWarningLogic.resolve(state)
        assertEquals(HomeWarningLogic.Kind.SERVER_UNREACHABLE, warning.kind)
        assertTrue(warning.isVisible)
        assertTrue(warning.isServerConnectionFailure)
        assertFalse(warning.isSmartThingsError)
    }

    @Test
    fun resolve_tailscaleDisconnected_distinguishedFromSmartThingsError() {
        val state = ConnectionState.Failed(
            error = ConnectionError.UNREACHABLE,
            message = TailscaleDiagnoser.TAILSCALE_VPN_DISCONNECTED_MSG
        )
        val warning = HomeWarningLogic.resolve(state)
        assertEquals(HomeWarningLogic.Kind.TAILSCALE_DISCONNECTED, warning.kind)
        assertTrue(warning.isVisible)
        assertTrue(warning.isServerConnectionFailure)
        assertFalse(warning.isSmartThingsError)
    }

    @Test
    fun resolve_sharedSecretUnauthorized_doesNotShowCardWarning() {
        val state = ConnectionState.Failed(
            error = ConnectionError.UNAUTHORIZED,
            message = "인증 실패: 공유 비밀키가 올바르지 않습니다."
        )
        val warning = HomeWarningLogic.resolve(state)
        assertEquals(HomeWarningLogic.Kind.NONE, warning.kind)
        assertFalse(warning.isVisible)
    }

    @Test
    fun formatSmartThingsMessage_includesOptionalDetailAndLastSuccess() {
        val full = HomeWarningLogic.formatSmartThingsMessage(
            "기본 안내",
            "HTTP 401",
            "2026-11-19T09:00:00+09:00"
        )
        assertTrue(full.contains("기본 안내"))
        assertTrue(full.contains("상세 오류: HTTP 401"))
        assertTrue(full.contains("마지막 정상 연동: 2026-11-19T09:00:00+09:00"))

        val minimal = HomeWarningLogic.formatSmartThingsMessage("기본 안내", "", null)
        assertEquals("기본 안내", minimal)
    }
}
