package com.smartthings.cleaner.network

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import java.net.NetworkInterface
import java.net.URI

/**
 * 서버 주소의 Tailscale 대역 여부 및 Tailscale VPN 연결 상태를 진단한다 (REQ-025, LB-056).
 *
 * - Tailscale IP 대역: RFC 6598 CGNAT 대역 (100.64.0.0/10: 100.64.0.0 ~ 100.127.255.255)
 * - Tailscale 도메인: tailscale, *.ts.net, *.tailscale 등
 * - VPN 감지: ConnectivityManager TRANSPORT_VPN 또는 NetworkInterface (tun, tap, tailscale)
 */
open class TailscaleDiagnoser(
    private val vpnConnectionChecker: (Context?) -> Boolean = { defaultCheckVpnConnected(it) }
) {
    companion object {
        const val TAILSCALE_VPN_DISCONNECTED_MSG =
            "Tailscale이 연결되어 있지 않습니다. Tailscale 앱을 실행하고 연결을 켠 뒤 다시 시도해 주세요."

        fun isTailscaleMessage(message: String?): Boolean =
            message != null && message.contains("Tailscale")

        /**
         * 주소(URL 또는 호스트명)가 Tailscale 대역(100.64.0.0/10) 또는 Tailscale 도메인인지 판별한다.
         */
        fun isTailscaleAddress(urlOrHost: String): Boolean {
            val host = extractHost(urlOrHost)
            if (host.isEmpty()) return false
            return isTailscaleIp(host) || isTailscaleDomain(host)
        }

        /**
         * IPv4 주소가 RFC 6598 CGNAT 대역 (100.64.0.0 ~ 100.127.255.255)인지 판별한다.
         */
        fun isTailscaleIp(host: String): Boolean {
            val parts = host.split(".")
            if (parts.size != 4) return false
            val nums = parts.map { it.toIntOrNull() ?: return false }
            if (nums.any { it !in 0..255 }) return false
            return nums[0] == 100 && nums[1] in 64..127
        }

        /**
         * 도메인이 Tailscale 관련 도메인인지 판별한다. (*.ts.net, tailscale 등)
         */
        fun isTailscaleDomain(host: String): Boolean {
            val h = host.lowercase()
            if (h.isEmpty()) return false
            return h == "tailscale" ||
                h.endsWith(".tailscale") ||
                h == "ts.net" ||
                h.endsWith(".ts.net") ||
                h.contains("tailscale")
        }

        /**
         * URL 문자열 또는 호스트 문자열에서 호스트명만 추출한다.
         */
        fun extractHost(urlOrHost: String): String {
            val trimmed = urlOrHost.trim()
            if (trimmed.isEmpty()) return ""
            return try {
                val candidate = if (trimmed.contains("://")) trimmed else "http://$trimmed"
                val uri = URI(candidate)
                uri.host?.lowercase() ?: fallbackExtractHost(trimmed)
            } catch (_: Throwable) {
                fallbackExtractHost(trimmed)
            }
        }

        private fun fallbackExtractHost(raw: String): String {
            var s = raw.substringAfter("://").substringBefore("/").substringBefore("?").substringBefore("#")
            if (s.contains("@")) s = s.substringAfter("@")
            if (s.contains(":")) s = s.substringBefore(":")
            return s.trim().lowercase()
        }

        /**
         * 기기에서 VPN(Tailscale 등)이 활성화되어 있는지 확인한다.
         */
        @Suppress("DEPRECATION")
        fun defaultCheckVpnConnected(context: Context?): Boolean {
            if (context != null) {
                try {
                    val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
                    if (cm != null) {
                        val active = cm.activeNetwork
                        if (active != null) {
                            val caps = cm.getNetworkCapabilities(active)
                            if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                                return true
                            }
                        }
                        for (network in cm.allNetworks) {
                            val caps = cm.getNetworkCapabilities(network)
                            if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                                return true
                            }
                        }
                    }
                } catch (_: Throwable) {
                    // Context/ConnectivityManager 조회 실패 시 NetworkInterface 확인으로 진행
                }
            }

            try {
                val interfaces = NetworkInterface.getNetworkInterfaces()
                if (interfaces != null) {
                    for (iface in interfaces) {
                        if (!iface.isUp) continue
                        val name = iface.name.lowercase()
                        if (name.startsWith("tun") || name.startsWith("tap") || name.contains("tailscale")) {
                            return true
                        }
                    }
                }
            } catch (_: Throwable) {
                // 무시
            }

            return false
        }
    }

    /**
     * VPN 활성화 여부를 진단한다.
     */
    open fun isVpnConnected(context: Context?): Boolean = vpnConnectionChecker(context)

    /**
     * 연결 실패 상태를 진단하여, Tailscale 주소인데 VPN이 꺼져 있는 경우 Tailscale 활성화 안내 메시지로 변환한다.
     * 일반 LAN/외부 도메인이거나 VPN이 켜져 있는 상태의 실패는 기존 실패 메시지를 유지한다.
     */
    fun diagnose(
        serverUrl: String,
        failure: ConnectionState.Failed,
        context: Context? = null
    ): ConnectionState.Failed {
        if (failure.error != ConnectionError.UNREACHABLE) {
            return failure
        }
        if (isTailscaleAddress(serverUrl) && !isVpnConnected(context)) {
            return ConnectionState.Failed(ConnectionError.UNREACHABLE, TAILSCALE_VPN_DISCONNECTED_MSG)
        }
        return failure
    }
}
