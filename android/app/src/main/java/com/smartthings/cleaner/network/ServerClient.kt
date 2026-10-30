package com.smartthings.cleaner.network

import android.content.Context
import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import com.smartthings.cleaner.model.CreateReservationResult
import org.json.JSONObject
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStream
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.MalformedURLException
import java.net.URL

/**
 * 미니PC 로봇청소기 서버 통신 클라이언트.
 *
 * - SPEC D-01: 안드로이드 네이티브 앱 (APK 직접 설치)
 * - SPEC D-09: 두 사용자 모두 동일한 공유 비밀키 사용
 * - SPEC REQ-024: 공유 비밀키(X-Shared-Secret / Bearer) 인증
 * - SPEC REQ-025: 미니PC 서버 연결 불가 경고
 */
class ServerClient(
    private val connectTimeoutMs: Int = 5000,
    private val readTimeoutMs: Int = 5000
) {
    companion object {
        private const val PREFS_NAME = "smartthings_cleaner_prefs"
        private const val KEY_SERVER_URL = "key_server_url"
        private const val KEY_SHARED_SECRET = "key_shared_secret"

        fun normalizeUrl(rawUrl: String): String {
            var trimmed = rawUrl.trim()
            if (trimmed.endsWith("/")) {
                trimmed = trimmed.substring(0, trimmed.length - 1)
            }
            return trimmed
        }

        fun saveCredentials(context: Context, serverUrl: String, sharedSecret: String) {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            prefs.edit()
                .putString(KEY_SERVER_URL, normalizeUrl(serverUrl))
                .putString(KEY_SHARED_SECRET, sharedSecret.trim())
                .apply()
        }

        fun getSavedServerUrl(context: Context): String {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            return prefs.getString(KEY_SERVER_URL, "") ?: ""
        }

        fun getSavedSharedSecret(context: Context): String {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            return prefs.getString(KEY_SHARED_SECRET, "") ?: ""
        }
    }

    /**
     * 서버 연결 상태를 검증한다.
     * 1) 유효성 검사 (빈 값, URL 형식)
     * 2) /health 헬스체크 (상태 확인 주소, 인증 제외)
     * 3) /api/reservations 인증 요청 (공유 비밀키 헤더 검증)
     */
    fun testConnection(serverUrl: String, sharedSecret: String): ConnectionState {
        val cleanUrl = normalizeUrl(serverUrl)
        val cleanSecret = sharedSecret.trim()

        if (cleanUrl.isEmpty()) {
            return ConnectionState.Failed(ConnectionError.EMPTY_URL, "서버 주소를 입력해 주세요.")
        }
        if (cleanSecret.isEmpty()) {
            return ConnectionState.Failed(ConnectionError.EMPTY_SECRET, "공유 비밀키를 입력해 주세요.")
        }
        if (!cleanUrl.startsWith("http://") && !cleanUrl.startsWith("https://")) {
            return ConnectionState.Failed(ConnectionError.INVALID_URL, "URL은 http:// 또는 https://로 시작해야 합니다.")
        }

        // 1단계: /health 헬스체크
        val healthResult = checkHealth(cleanUrl)
        if (healthResult is HealthCheckResult.Failure) {
            return ConnectionState.Failed(ConnectionError.UNREACHABLE, healthResult.message)
        }
        val health = (healthResult as HealthCheckResult.Success)

        // 2단계: /api/reservations 비밀키 인증 확인
        val authResult = checkAuth(cleanUrl, cleanSecret)
        return when (authResult) {
            is AuthCheckResult.Success -> ConnectionState.Connected(
                serverUrl = cleanUrl,
                serviceName = health.serviceName,
                serverTimeKst = health.serverTimeKst,
                reservationCount = authResult.reservationCount
            )
            is AuthCheckResult.Unauthorized -> ConnectionState.Failed(
                ConnectionError.UNAUTHORIZED,
                "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)"
            )
            is AuthCheckResult.Failure -> ConnectionState.Failed(
                ConnectionError.UNREACHABLE,
                authResult.message
            )
        }
    }

    private fun checkHealth(baseUrl: String): HealthCheckResult {
        var conn: HttpURLConnection? = null
        return try {
            val targetUrl = URL("$baseUrl/health")
            conn = (targetUrl.openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                connectTimeout = connectTimeoutMs
                readTimeout = readTimeoutMs
                instanceFollowRedirects = true
            }
            val code = conn.responseCode
            if (code == HttpURLConnection.HTTP_OK) {
                val body = readStream(conn.inputStream)
                val json = JSONObject(body)
                val service = json.optString("service", "smart-things-mini-pc")
                val timeKst = json.optString("time_kst", "-")
                HealthCheckResult.Success(service, timeKst)
            } else {
                HealthCheckResult.Failure("서버 응답 오류 (HTTP $code)")
            }
        } catch (e: MalformedURLException) {
            HealthCheckResult.Failure("잘못된 서버 주소 형식입니다.")
        } catch (e: IOException) {
            HealthCheckResult.Failure("서버에 연결할 수 없습니다: ${e.message}")
        } catch (e: Exception) {
            HealthCheckResult.Failure("헬스체크 중 오류: ${e.message}")
        } finally {
            conn?.disconnect()
        }
    }

    private fun checkAuth(baseUrl: String, secret: String): AuthCheckResult {
        var conn: HttpURLConnection? = null
        return try {
            val targetUrl = URL("$baseUrl/api/reservations")
            conn = (targetUrl.openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                connectTimeout = connectTimeoutMs
                readTimeout = readTimeoutMs
                setRequestProperty("X-Shared-Secret", secret)
                setRequestProperty("Authorization", "Bearer $secret")
            }
            val code = conn.responseCode
            when (code) {
                HttpURLConnection.HTTP_OK -> {
                    val body = readStream(conn.inputStream)
                    val json = JSONObject(body)
                    val count = json.optJSONArray("reservations")?.length() ?: 0
                    AuthCheckResult.Success(count)
                }
                HttpURLConnection.HTTP_UNAUTHORIZED -> {
                    AuthCheckResult.Unauthorized
                }
                else -> {
                    AuthCheckResult.Failure("예상치 못한 응답 코드: HTTP $code")
                }
            }
        } catch (e: IOException) {
            AuthCheckResult.Failure("인증 확인 중 통신 실패: ${e.message}")
        } catch (e: Exception) {
            AuthCheckResult.Failure("인증 확인 오류: ${e.message}")
        } finally {
            conn?.disconnect()
        }
    }

    /**
     * 1회성 청소 예약을 서버에 등록한다 (POST /api/reservations).
     *
     * - REQ-001: 분 단위 미래 일시 KST ISO 8601 문자열 전송
     * - D-03: 모드·구역 필드 절대 포함하지 않음 (기본 전체 청소)
     * - D-09 / REQ-024: 공유 비밀키 인증 (X-Shared-Secret, Authorization: Bearer)
     */
    fun createReservation(
        serverUrl: String,
        sharedSecret: String,
        scheduledAtIso: String
    ): CreateReservationResult {
        val cleanUrl = normalizeUrl(serverUrl)
        val cleanSecret = sharedSecret.trim()
        val cleanScheduledAt = scheduledAtIso.trim()

        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) {
            return CreateReservationResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        }
        if (cleanScheduledAt.isEmpty()) {
            return CreateReservationResult.Failure(400, "예약 일시가 지정되지 않았습니다.")
        }

        var conn: HttpURLConnection? = null
        return try {
            val targetUrl = URL("$cleanUrl/api/reservations")
            conn = (targetUrl.openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = connectTimeoutMs
                readTimeout = readTimeoutMs
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=UTF-8")
                setRequestProperty("X-Shared-Secret", cleanSecret)
                setRequestProperty("Authorization", "Bearer $cleanSecret")
            }

            // D-03: 청소 모드 및 구역 필드 배제 (기본 전체 청소)
            val payload = JSONObject().apply {
                put("scheduled_at", cleanScheduledAt)
            }

            OutputStreamWriter(conn.outputStream, "UTF-8").use { writer ->
                writer.write(payload.toString())
                writer.flush()
            }

            val code = conn.responseCode
            when (code) {
                HttpURLConnection.HTTP_CREATED, HttpURLConnection.HTTP_OK -> {
                    val body = readStream(conn.inputStream)
                    val json = JSONObject(body)
                    val id = json.optString("id", "")
                    val scheduledAt = json.optString("scheduled_at", json.optString("reserved_at", cleanScheduledAt))
                    val status = json.optString("status", "예정")
                    CreateReservationResult.Success(id, scheduledAt, status)
                }
                HttpURLConnection.HTTP_BAD_REQUEST -> {
                    val stream = conn.errorStream ?: conn.inputStream
                    val body = if (stream != null) readStream(stream) else ""
                    val msg = try {
                        JSONObject(body).optString("error", "잘못된 요청입니다.")
                    } catch (e: Exception) {
                        "과거 시각 또는 잘못된 형식입니다: HTTP 400"
                    }
                    CreateReservationResult.Failure(400, msg)
                }
                HttpURLConnection.HTTP_UNAUTHORIZED -> {
                    CreateReservationResult.Failure(
                        401,
                        "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)"
                    )
                }
                else -> {
                    CreateReservationResult.Failure(code, "서버 응답 오류 (HTTP $code)")
                }
            }
        } catch (e: IOException) {
            CreateReservationResult.Failure(0, "서버에 연결할 수 없습니다: ${e.message}")
        } catch (e: Exception) {
            CreateReservationResult.Failure(0, "예약 생성 중 오류: ${e.message}")
        } finally {
            conn?.disconnect()
        }
    }

    private fun readStream(inputStream: InputStream): String {
        return BufferedReader(InputStreamReader(inputStream, "UTF-8")).use { reader ->
            val sb = StringBuilder()
            var line: String?
            while (reader.readLine().also { line = it } != null) {
                sb.append(line)
            }
            sb.toString()
        }
    }

    private sealed class HealthCheckResult {
        data class Success(val serviceName: String, val serverTimeKst: String) : HealthCheckResult()
        data class Failure(val message: String) : HealthCheckResult()
    }

    private sealed class AuthCheckResult {
        data class Success(val reservationCount: Int) : AuthCheckResult()
        object Unauthorized : AuthCheckResult()
        data class Failure(val message: String) : AuthCheckResult()
    }
}
