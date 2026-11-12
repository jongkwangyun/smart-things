package com.smartthings.cleaner.network

import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TailscaleDiagnoserTest {

    @Test
    fun testIsTailscaleIp_cgnatRangeBoundary() {
        // RFC 6598 CGNAT 대역: 100.64.0.0/10 (100.64.0.0 ~ 100.127.255.255)
        assertTrue(TailscaleDiagnoser.isTailscaleIp("100.64.0.0"))
        assertTrue(TailscaleDiagnoser.isTailscaleIp("100.64.0.1"))
        assertTrue(TailscaleDiagnoser.isTailscaleIp("100.100.50.25"))
        assertTrue(TailscaleDiagnoser.isTailscaleIp("100.127.255.254"))
        assertTrue(TailscaleDiagnoser.isTailscaleIp("100.127.255.255"))

        // 대역 밖 (100.63.x.x 또는 100.128.x.x)
        assertFalse(TailscaleDiagnoser.isTailscaleIp("100.63.255.255"))
        assertFalse(TailscaleDiagnoser.isTailscaleIp("100.128.0.0"))
        assertFalse(TailscaleDiagnoser.isTailscaleIp("100.200.1.1"))
    }

    @Test
    fun testIsTailscaleIp_nonTailscaleAndInvalid() {
        // 일반 LAN 및 로컬
        assertFalse(TailscaleDiagnoser.isTailscaleIp("192.168.0.1"))
        assertFalse(TailscaleDiagnoser.isTailscaleIp("10.0.0.1"))
        assertFalse(TailscaleDiagnoser.isTailscaleIp("172.16.0.1"))
        assertFalse(TailscaleDiagnoser.isTailscaleIp("127.0.0.1"))

        // 잘못된 형식
        assertFalse(TailscaleDiagnoser.isTailscaleIp(""))
        assertFalse(TailscaleDiagnoser.isTailscaleIp("abc"))
        assertFalse(TailscaleDiagnoser.isTailscaleIp("100.64.0"))
        assertFalse(TailscaleDiagnoser.isTailscaleIp("100.64.0.0.1"))
        assertFalse(TailscaleDiagnoser.isTailscaleIp("100.64.0.256"))
    }

    @Test
    fun testIsTailscaleDomain() {
        // Tailscale 도메인 (*.ts.net, tailscale)
        assertTrue(TailscaleDiagnoser.isTailscaleDomain("mini-pc.ts.net"))
        assertTrue(TailscaleDiagnoser.isTailscaleDomain("ts.net"))
        assertTrue(TailscaleDiagnoser.isTailscaleDomain("sub.node.ts.net"))
        assertTrue(TailscaleDiagnoser.isTailscaleDomain("cleaner.tailscale"))
        assertTrue(TailscaleDiagnoser.isTailscaleDomain("tailscale"))
        assertTrue(TailscaleDiagnoser.isTailscaleDomain("server.tailscale.internal"))

        // 일반 도메인 및 기타
        assertFalse(TailscaleDiagnoser.isTailscaleDomain("cleaner.example.com"))
        assertFalse(TailscaleDiagnoser.isTailscaleDomain("google.com"))
        assertFalse(TailscaleDiagnoser.isTailscaleDomain("home.duckdns.org"))
        assertFalse(TailscaleDiagnoser.isTailscaleDomain("192.168.1.1"))
        assertFalse(TailscaleDiagnoser.isTailscaleDomain(""))
    }

    @Test
    fun testExtractHost() {
        assertEquals("100.85.12.34", TailscaleDiagnoser.extractHost("http://100.85.12.34:8080"))
        assertEquals("100.85.12.34", TailscaleDiagnoser.extractHost("https://100.85.12.34:8080/health"))
        assertEquals("mini-pc.ts.net", TailscaleDiagnoser.extractHost("http://mini-pc.ts.net:18765"))
        assertEquals("mini-pc.ts.net", TailscaleDiagnoser.extractHost("mini-pc.ts.net:18765"))
        assertEquals("100.64.1.2", TailscaleDiagnoser.extractHost("100.64.1.2"))
        assertEquals("", TailscaleDiagnoser.extractHost(""))
    }

    @Test
    fun testIsTailscaleAddress() {
        assertTrue(TailscaleDiagnoser.isTailscaleAddress("http://100.85.1.2:8080"))
        assertTrue(TailscaleDiagnoser.isTailscaleAddress("https://cleaner-server.ts.net"))
        assertTrue(TailscaleDiagnoser.isTailscaleAddress("100.64.0.5:18765"))
        assertTrue(TailscaleDiagnoser.isTailscaleAddress("http://mini-pc.tailscale:8080"))

        // 일반 LAN / 외부 도메인은 false
        assertFalse(TailscaleDiagnoser.isTailscaleAddress("http://192.168.1.100:8080"))
        assertFalse(TailscaleDiagnoser.isTailscaleAddress("http://10.0.0.5:8080"))
        assertFalse(TailscaleDiagnoser.isTailscaleAddress("https://cleaner.example.com:8443"))
    }

    @Test
    fun testDiagnose_tailscaleAddress_vpnDisconnected_returnsTailscaleMessage() {
        val diagnoser = TailscaleDiagnoser(vpnConnectionChecker = { false })
        val originalFailure = ConnectionState.Failed(ConnectionError.UNREACHABLE, "서버에 연결할 수 없습니다: Connection refused")

        val result = diagnoser.diagnose("http://100.85.1.2:8080", originalFailure, context = null)

        assertEquals(ConnectionError.UNREACHABLE, result.error)
        assertEquals(TailscaleDiagnoser.TAILSCALE_VPN_DISCONNECTED_MSG, result.message)
        assertTrue(TailscaleDiagnoser.isTailscaleMessage(result.message))
    }

    @Test
    fun testDiagnose_tailscaleAddress_vpnConnected_retainsOriginalMessage() {
        val diagnoser = TailscaleDiagnoser(vpnConnectionChecker = { true })
        val originalFailure = ConnectionState.Failed(ConnectionError.UNREACHABLE, "서버에 연결할 수 없습니다: Connection timed out")

        val result = diagnoser.diagnose("http://100.85.1.2:8080", originalFailure, context = null)

        assertEquals(ConnectionError.UNREACHABLE, result.error)
        assertEquals("서버에 연결할 수 없습니다: Connection timed out", result.message)
        assertFalse(TailscaleDiagnoser.isTailscaleMessage(result.message))
    }

    @Test
    fun testDiagnose_lanAddress_vpnDisconnected_retainsOriginalMessage() {
        val diagnoser = TailscaleDiagnoser(vpnConnectionChecker = { false })
        val originalFailure = ConnectionState.Failed(ConnectionError.UNREACHABLE, "서버에 연결할 수 없습니다: No route to host")

        val result = diagnoser.diagnose("http://192.168.1.100:8080", originalFailure, context = null)

        assertEquals(ConnectionError.UNREACHABLE, result.error)
        assertEquals("서버에 연결할 수 없습니다: No route to host", result.message)
    }

    @Test
    fun testDiagnose_externalDomain_vpnDisconnected_retainsOriginalMessage() {
        val diagnoser = TailscaleDiagnoser(vpnConnectionChecker = { false })
        val originalFailure = ConnectionState.Failed(ConnectionError.UNREACHABLE, "서버에 연결할 수 없습니다: Host not found")

        val result = diagnoser.diagnose("https://cleaner.example.com", originalFailure, context = null)

        assertEquals(ConnectionError.UNREACHABLE, result.error)
        assertEquals("서버에 연결할 수 없습니다: Host not found", result.message)
    }

    @Test
    fun testDiagnose_nonUnreachableError_retainsOriginalError() {
        val diagnoser = TailscaleDiagnoser(vpnConnectionChecker = { false })
        val authFailure = ConnectionState.Failed(ConnectionError.UNAUTHORIZED, "인증 실패: 공유 비밀키가 올바르지 않습니다.")

        val result = diagnoser.diagnose("http://100.85.1.2:8080", authFailure, context = null)

        assertEquals(ConnectionError.UNAUTHORIZED, result.error)
        assertEquals("인증 실패: 공유 비밀키가 올바르지 않습니다.", result.message)
    }

    @Test
    fun testServerClient_testConnection_tailscaleVpnDisconnected() {
        val diagnoser = TailscaleDiagnoser(vpnConnectionChecker = { false })
        // Port 1 on non-existent Tailscale IP will fail connection immediately
        val client = ServerClient(connectTimeoutMs = 500, readTimeoutMs = 500, tailscaleDiagnoser = diagnoser)

        val result = client.testConnection("http://100.115.1.1:1", "secret", context = null)

        assertTrue(result is ConnectionState.Failed)
        val failed = result as ConnectionState.Failed
        assertEquals(ConnectionError.UNREACHABLE, failed.error)
        assertEquals(TailscaleDiagnoser.TAILSCALE_VPN_DISCONNECTED_MSG, failed.message)
    }

    @Test
    fun testServerClient_testConnection_lanVpnDisconnected_keepsNormalError() {
        val diagnoser = TailscaleDiagnoser(vpnConnectionChecker = { false })
        val client = ServerClient(connectTimeoutMs = 500, readTimeoutMs = 500, tailscaleDiagnoser = diagnoser)

        val result = client.testConnection("http://192.168.254.254:1", "secret", context = null)

        assertTrue(result is ConnectionState.Failed)
        val failed = result as ConnectionState.Failed
        assertEquals(ConnectionError.UNREACHABLE, failed.error)
        assertFalse(TailscaleDiagnoser.isTailscaleMessage(failed.message))
    }
}
