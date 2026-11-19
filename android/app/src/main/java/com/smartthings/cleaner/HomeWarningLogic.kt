package com.smartthings.cleaner

import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import com.smartthings.cleaner.network.TailscaleDiagnoser

/**
 * 홈 화면 경고 카드 상태 분류 및 메시지 포맷 로직 (LB-029, LB-056, LB-064).
 * Android 의존성 없음(JVM 단위 테스트 대상).
 */
object HomeWarningLogic {

    enum class Kind {
        NONE,
        SERVER_UNREACHABLE,
        TAILSCALE_DISCONNECTED,
        SMARTTHINGS_AUTH_FAILED,
        SMARTTHINGS_API_ERROR
    }

    data class WarningState(
        val kind: Kind,
        val detail: String = "",
        val lastSuccessAt: String? = null
    ) {
        val isVisible: Boolean
            get() = kind != Kind.NONE

        val isServerConnectionFailure: Boolean
            get() = kind == Kind.SERVER_UNREACHABLE || kind == Kind.TAILSCALE_DISCONNECTED

        val isSmartThingsError: Boolean
            get() = kind == Kind.SMARTTHINGS_AUTH_FAILED || kind == Kind.SMARTTHINGS_API_ERROR
    }

    fun resolve(state: ConnectionState): WarningState {
        if (state is ConnectionState.Failed && state.error == ConnectionError.UNREACHABLE) {
            val kind = if (TailscaleDiagnoser.isTailscaleMessage(state.message)) {
                Kind.TAILSCALE_DISCONNECTED
            } else {
                Kind.SERVER_UNREACHABLE
            }
            return WarningState(kind = kind, detail = state.message)
        }
        if (state is ConnectionState.Connected && !state.smartthingsOk) {
            val kind = if (state.smartthingsAuthState == "auth_failed") {
                Kind.SMARTTHINGS_AUTH_FAILED
            } else {
                Kind.SMARTTHINGS_API_ERROR
            }
            return WarningState(
                kind = kind,
                detail = state.smartthingsLastError.orEmpty().trim(),
                lastSuccessAt = state.smartthingsLastSuccessAt?.trim()?.ifEmpty { null }
            )
        }
        return WarningState(kind = Kind.NONE)
    }

    fun formatSmartThingsMessage(
        baseMessage: String,
        detail: String,
        lastSuccessAt: String?
    ): String = buildString {
        append(baseMessage)
        if (detail.isNotBlank()) {
            append("\n상세 오류: ").append(detail)
        }
        if (!lastSuccessAt.isNullOrBlank()) {
            append("\n마지막 정상 연동: ").append(lastSuccessAt)
        }
    }
}
