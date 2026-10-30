package com.smartthings.cleaner.model

/**
 * 미니PC 서버와의 연결 상태를 나타내는 모델.
 * SPEC D-01 (APK 직접 설치), D-09 (공유 비밀키), REQ-024 (인증), REQ-025 (연결 불가 경고).
 */
sealed class ConnectionState {
    object Disconnected : ConnectionState()

    object Connecting : ConnectionState()

    data class Connected(
        val serverUrl: String,
        val serviceName: String,
        val serverTimeKst: String,
        val reservationCount: Int
    ) : ConnectionState()

    data class Failed(
        val error: ConnectionError,
        val message: String
    ) : ConnectionState()
}

/**
 * 서버 연결 실패 유형.
 */
enum class ConnectionError {
    EMPTY_URL,
    EMPTY_SECRET,
    INVALID_URL,
    UNAUTHORIZED,  // 401: 공유 비밀키 불일치 (D-09, REQ-024)
    UNREACHABLE,   // 연결 불가, 호스트 미발견, 타임아웃 (REQ-025)
    SERVER_ERROR,  // 5xx 서버 오류
    UNKNOWN        // 기타 알 수 없는 오류
}
