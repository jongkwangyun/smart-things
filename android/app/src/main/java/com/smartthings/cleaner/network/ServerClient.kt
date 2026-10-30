package com.smartthings.cleaner.network

import android.content.Context
import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import com.smartthings.cleaner.model.CreateOffPeriodResult
import com.smartthings.cleaner.model.CreateReservationResult
import com.smartthings.cleaner.model.FetchOffPeriodsResult
import com.smartthings.cleaner.model.FetchReservationsResult
import com.smartthings.cleaner.model.OffPeriodItem
import com.smartthings.cleaner.model.ReservationItem
import org.json.JSONArray
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
 * SPEC: D-01, D-09/REQ-024, REQ-002, REQ-011, REQ-012, REQ-025.
 */
class ServerClient(
    private val connectTimeoutMs: Int = 5000,
    private val readTimeoutMs: Int = 5000
) {
    companion object {
        private const val PREFS_NAME = "smartthings_cleaner_prefs"
        private const val KEY_SERVER_URL = "key_server_url"
        private const val KEY_SHARED_SECRET = "key_shared_secret"

        fun normalizeUrl(rawUrl: String): String = rawUrl.trim().removeSuffix("/")

        fun saveCredentials(context: Context, serverUrl: String, sharedSecret: String) {
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
                .putString(KEY_SERVER_URL, normalizeUrl(serverUrl))
                .putString(KEY_SHARED_SECRET, sharedSecret.trim())
                .apply()
        }

        fun getSavedServerUrl(context: Context): String =
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).getString(KEY_SERVER_URL, "") ?: ""

        fun getSavedSharedSecret(context: Context): String =
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).getString(KEY_SHARED_SECRET, "") ?: ""
    }

    private fun openAuthConnection(endpoint: String, secret: String, method: String): HttpURLConnection {
        return (URL(endpoint).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = connectTimeoutMs
            readTimeout = readTimeoutMs
            setRequestProperty("X-Shared-Secret", secret)
            setRequestProperty("Authorization", "Bearer $secret")
        }
    }

    private fun readErrorMsg(conn: HttpURLConnection, fallback: String): String {
        return try {
            val stream = conn.errorStream ?: conn.inputStream ?: return fallback
            JSONObject(readStream(stream)).optString("error", fallback)
        } catch (_: Exception) { fallback }
    }

    /** 서버 연결 상태를 검증한다 (REQ-024, REQ-025). */
    fun testConnection(serverUrl: String, sharedSecret: String): ConnectionState {
        val cleanUrl = normalizeUrl(serverUrl)
        val cleanSecret = sharedSecret.trim()

        if (cleanUrl.isEmpty()) return ConnectionState.Failed(ConnectionError.EMPTY_URL, "서버 주소를 입력해 주세요.")
        if (cleanSecret.isEmpty()) return ConnectionState.Failed(ConnectionError.EMPTY_SECRET, "공유 비밀키를 입력해 주세요.")
        if (!cleanUrl.startsWith("http://") && !cleanUrl.startsWith("https://")) {
            return ConnectionState.Failed(ConnectionError.INVALID_URL, "URL은 http:// 또는 https://로 시작해야 합니다.")
        }

        val healthResult = checkHealth(cleanUrl)
        if (healthResult is HealthCheckResult.Failure) {
            return ConnectionState.Failed(ConnectionError.UNREACHABLE, healthResult.message)
        }
        val health = healthResult as HealthCheckResult.Success

        return when (val authResult = checkAuth(cleanUrl, cleanSecret)) {
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
            is AuthCheckResult.Failure -> ConnectionState.Failed(ConnectionError.UNREACHABLE, authResult.message)
        }
    }

    private fun checkHealth(baseUrl: String): HealthCheckResult {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL("$baseUrl/health").openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                connectTimeout = connectTimeoutMs
                readTimeout = readTimeoutMs
                instanceFollowRedirects = true
            }
            if (conn.responseCode == HttpURLConnection.HTTP_OK) {
                val json = JSONObject(readStream(conn.inputStream))
                HealthCheckResult.Success(json.optString("service", "smart-things-mini-pc"), json.optString("time_kst", "-"))
            } else {
                HealthCheckResult.Failure("서버 응답 오류 (HTTP ${conn.responseCode})")
            }
        } catch (e: Exception) {
            HealthCheckResult.Failure(if (e is MalformedURLException) "잘못된 서버 주소 형식입니다." else "서버에 연결할 수 없습니다: ${e.message}")
        } finally {
            conn?.disconnect()
        }
    }

    private fun checkAuth(baseUrl: String, secret: String): AuthCheckResult {
        return when (val result = fetchReservations(baseUrl, secret)) {
            is FetchReservationsResult.Success -> AuthCheckResult.Success(result.reservations.size)
            is FetchReservationsResult.Failure -> {
                if (result.statusCode == 401) AuthCheckResult.Unauthorized
                else AuthCheckResult.Failure(result.message)
            }
        }
    }

    /** 1회성 청소 예약을 서버에 등록한다 (POST /api/reservations, REQ-001, D-03). */
    fun createReservation(serverUrl: String, sharedSecret: String, scheduledAtIso: String): CreateReservationResult {
        val cleanUrl = normalizeUrl(serverUrl)
        val cleanSecret = sharedSecret.trim()
        val cleanScheduledAt = scheduledAtIso.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return CreateReservationResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        if (cleanScheduledAt.isEmpty()) return CreateReservationResult.Failure(400, "예약 일시가 지정되지 않았습니다.")

        var conn: HttpURLConnection? = null
        return try {
            conn = openAuthConnection("$cleanUrl/api/reservations", cleanSecret, "POST").apply {
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=UTF-8")
            }
            val payload = JSONObject().apply { put("scheduled_at", cleanScheduledAt) }
            OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write(payload.toString()) }

            when (val code = conn.responseCode) {
                HttpURLConnection.HTTP_CREATED, HttpURLConnection.HTTP_OK -> {
                    val json = JSONObject(readStream(conn.inputStream))
                    CreateReservationResult.Success(
                        json.optString("id", ""),
                        json.optString("scheduled_at", json.optString("reserved_at", cleanScheduledAt)),
                        json.optString("status", "예정")
                    )
                }
                HttpURLConnection.HTTP_BAD_REQUEST -> CreateReservationResult.Failure(400, readErrorMsg(conn, "과거 시각 또는 잘못된 형식입니다: HTTP 400"))
                HttpURLConnection.HTTP_UNAUTHORIZED -> CreateReservationResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> CreateReservationResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            CreateReservationResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "예약 생성 중 오류: ${e.message}")
        } finally {
            conn?.disconnect()
        }
    }

    /** 쉬는 기간을 서버에 등록한다 (POST /api/off-periods, REQ-011). */
    fun createOffPeriod(serverUrl: String, sharedSecret: String, startAtIso: String, endAtIso: String): CreateOffPeriodResult {
        val cleanUrl = normalizeUrl(serverUrl)
        val cleanSecret = sharedSecret.trim()
        val cleanStart = startAtIso.trim()
        val cleanEnd = endAtIso.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return CreateOffPeriodResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        if (cleanStart.isEmpty() || cleanEnd.isEmpty()) return CreateOffPeriodResult.Failure(400, "시작 일시와 종료 일시가 필요합니다.")

        var conn: HttpURLConnection? = null
        return try {
            conn = openAuthConnection("$cleanUrl/api/off-periods", cleanSecret, "POST").apply {
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=UTF-8")
            }
            val payload = JSONObject().apply {
                put("start_at", cleanStart)
                put("end_at", cleanEnd)
            }
            OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write(payload.toString()) }

            when (val code = conn.responseCode) {
                HttpURLConnection.HTTP_CREATED, HttpURLConnection.HTTP_OK -> {
                    val json = JSONObject(readStream(conn.inputStream))
                    CreateOffPeriodResult.Success(json.optString("id", ""), json.optString("start_at", cleanStart), json.optString("end_at", cleanEnd))
                }
                HttpURLConnection.HTTP_BAD_REQUEST -> CreateOffPeriodResult.Failure(400, readErrorMsg(conn, "종료 일시는 시작 일시보다 뒤여야 합니다."))
                HttpURLConnection.HTTP_UNAUTHORIZED -> CreateOffPeriodResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> CreateOffPeriodResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            CreateOffPeriodResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "쉬는 기간 등록 중 오류: ${e.message}")
        } finally {
            conn?.disconnect()
        }
    }

    /** 예약 목록을 서버에서 조회한다 (GET /api/reservations, REQ-002). */
    fun fetchReservations(serverUrl: String, sharedSecret: String): FetchReservationsResult {
        val cleanUrl = normalizeUrl(serverUrl)
        val cleanSecret = sharedSecret.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return FetchReservationsResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")

        var conn: HttpURLConnection? = null
        return try {
            conn = openAuthConnection("$cleanUrl/api/reservations", cleanSecret, "GET")
            when (val code = conn.responseCode) {
                HttpURLConnection.HTTP_OK -> {
                    val array = JSONObject(readStream(conn.inputStream)).optJSONArray("reservations") ?: JSONArray()
                    val items = (0 until array.length()).map { ReservationItem.fromJson(array.getJSONObject(it)) }
                    FetchReservationsResult.Success(items)
                }
                HttpURLConnection.HTTP_UNAUTHORIZED -> FetchReservationsResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> FetchReservationsResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            FetchReservationsResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "예약 목록 조회 중 오류: ${e.message}")
        } finally {
            conn?.disconnect()
        }
    }

    /** 쉬는 기간 목록을 서버에서 조회한다 (GET /api/off-periods, REQ-012, REQ-010, D-09). */
    fun fetchOffPeriods(serverUrl: String, sharedSecret: String): FetchOffPeriodsResult {
        val cleanUrl = normalizeUrl(serverUrl)
        val cleanSecret = sharedSecret.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return FetchOffPeriodsResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")

        var conn: HttpURLConnection? = null
        return try {
            conn = openAuthConnection("$cleanUrl/api/off-periods", cleanSecret, "GET")
            when (val code = conn.responseCode) {
                HttpURLConnection.HTTP_OK -> {
                    val array = JSONObject(readStream(conn.inputStream)).optJSONArray("off_periods") ?: JSONArray()
                    val items = (0 until array.length()).map { OffPeriodItem.fromJson(array.getJSONObject(it)) }
                    FetchOffPeriodsResult.Success(items)
                }
                HttpURLConnection.HTTP_UNAUTHORIZED -> FetchOffPeriodsResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> FetchOffPeriodsResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            FetchOffPeriodsResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "쉬는 기간 목록 조회 중 오류: ${e.message}")
        } finally {
            conn?.disconnect()
        }
    }

    private fun readStream(inputStream: InputStream): String =
        BufferedReader(InputStreamReader(inputStream, Charsets.UTF_8)).use { it.readText() }

    private sealed class HealthCheckResult {
        data class Success(val serviceName: String, val serverTimeKst: String) : HealthCheckResult()
        data class Failure(val message: String) : HealthCheckResult()
    }

    private sealed class AuthCheckResult {
        data class Success(val reservationCount: Int) : AuthCheckResult()
        data object Unauthorized : AuthCheckResult()
        data class Failure(val message: String) : AuthCheckResult()
    }
}
