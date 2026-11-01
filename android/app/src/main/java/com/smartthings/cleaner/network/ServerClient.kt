package com.smartthings.cleaner.network

import android.content.Context
import com.smartthings.cleaner.model.CancelReservationResult
import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import com.smartthings.cleaner.model.CreateOffPeriodResult
import com.smartthings.cleaner.model.CreateReservationResult
import com.smartthings.cleaner.model.DeleteOffPeriodResult
import com.smartthings.cleaner.model.FetchOffPeriodsResult
import com.smartthings.cleaner.model.FetchReservationsResult
import com.smartthings.cleaner.model.OffPeriodItem
import com.smartthings.cleaner.model.RegisterNotificationResult
import com.smartthings.cleaner.model.ReservationItem
import com.smartthings.cleaner.model.SendNotificationResult
import com.smartthings.cleaner.model.UpdateReservationResult
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
 * 미니PC 로봇청소기 서버 통신 클라이언트 (D-01, D-09, D-12, REQ-022, REQ-024, REQ-025).
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
        private fun prefs(c: Context) = c.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        fun saveCredentials(context: Context, serverUrl: String, sharedSecret: String) =
            prefs(context).edit().putString(KEY_SERVER_URL, normalizeUrl(serverUrl)).putString(KEY_SHARED_SECRET, sharedSecret.trim()).apply()
        fun getSavedServerUrl(context: Context): String = prefs(context).getString(KEY_SERVER_URL, "") ?: ""
        fun getSavedSharedSecret(context: Context): String = prefs(context).getString(KEY_SHARED_SECRET, "") ?: ""
    }

    private fun openAuthConnection(endpoint: String, secret: String, method: String): HttpURLConnection =
        (URL(endpoint).openConnection() as HttpURLConnection).apply {
            requestMethod = method; connectTimeout = connectTimeoutMs; readTimeout = readTimeoutMs
            if (secret.isNotEmpty()) { setRequestProperty("X-Shared-Secret", secret); setRequestProperty("Authorization", "Bearer $secret") }
        }

    private fun sendJson(endpoint: String, secret: String, method: String, payload: JSONObject): Pair<Int, String> {
        var conn: HttpURLConnection? = null
        return try {
            conn = openAuthConnection(endpoint, secret, method).apply {
                doOutput = true; setRequestProperty("Content-Type", "application/json; charset=UTF-8")
            }
            OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write(payload.toString()) }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else (conn.errorStream ?: conn.inputStream)
            Pair(code, if (stream != null) readStream(stream) else "")
        } finally { conn?.disconnect() }
    }

    private fun postJson(endpoint: String, secret: String, payload: JSONObject) = sendJson(endpoint, secret, "POST", payload)
    private fun putJson(endpoint: String, secret: String, payload: JSONObject) = sendJson(endpoint, secret, "PUT", payload)
    private fun getJson(endpoint: String, secret: String) = requestNoBody(endpoint, secret, "GET")
    private fun deleteHttp(endpoint: String, secret: String) = requestNoBody(endpoint, secret, "DELETE")

    private fun requestNoBody(endpoint: String, secret: String, method: String): Pair<Int, String> {
        var conn: HttpURLConnection? = null
        return try {
            conn = openAuthConnection(endpoint, secret, method)
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else (conn.errorStream ?: conn.inputStream)
            Pair(code, if (stream != null) readStream(stream) else "")
        } finally { conn?.disconnect() }
    }

    private fun extractErrorMsg(jsonStr: String, fallback: String): String =
        try { JSONObject(jsonStr).optString("error", fallback) } catch (_: Exception) { fallback }

    /** 서버 연결 상태를 검증한다 (REQ-024, REQ-025). */
    fun testConnection(serverUrl: String, sharedSecret: String): ConnectionState {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim()
        if (cleanUrl.isEmpty()) return ConnectionState.Failed(ConnectionError.EMPTY_URL, "서버 주소를 입력해 주세요.")
        if (cleanSecret.isEmpty()) return ConnectionState.Failed(ConnectionError.EMPTY_SECRET, "공유 비밀키를 입력해 주세요.")
        if (!cleanUrl.startsWith("http://") && !cleanUrl.startsWith("https://")) return ConnectionState.Failed(ConnectionError.INVALID_URL, "URL은 http:// 또는 https://로 시작해야 합니다.")
        val healthResult = checkHealth(cleanUrl)
        if (healthResult is HealthCheckResult.Failure) return ConnectionState.Failed(ConnectionError.UNREACHABLE, healthResult.message)
        val health = healthResult as HealthCheckResult.Success

        return when (val authResult = checkAuth(cleanUrl, cleanSecret)) {
            is AuthCheckResult.Success -> ConnectionState.Connected(cleanUrl, health.serviceName, health.serverTimeKst, authResult.reservationCount)
            is AuthCheckResult.Unauthorized -> ConnectionState.Failed(ConnectionError.UNAUTHORIZED, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
            is AuthCheckResult.Failure -> ConnectionState.Failed(ConnectionError.UNREACHABLE, authResult.message)
        }
    }

    private fun checkHealth(baseUrl: String): HealthCheckResult = try {
        val (code, body) = getJson("$baseUrl/health", "")
        if (code == HttpURLConnection.HTTP_OK) JSONObject(body).let { HealthCheckResult.Success(it.optString("service", "smart-things-mini-pc"), it.optString("time_kst", "-")) }
        else HealthCheckResult.Failure("서버 응답 오류 (HTTP $code)")
    } catch (e: Exception) {
        HealthCheckResult.Failure(if (e is MalformedURLException) "잘못된 서버 주소 형식입니다." else "서버에 연결할 수 없습니다: ${e.message}")
    }

    private fun checkAuth(baseUrl: String, secret: String): AuthCheckResult =
        when (val result = fetchReservations(baseUrl, secret)) {
            is FetchReservationsResult.Success -> AuthCheckResult.Success(result.reservations.size)
            is FetchReservationsResult.Failure -> if (result.statusCode == 401) AuthCheckResult.Unauthorized else AuthCheckResult.Failure(result.message)
        }

    /** 1회성 청소 예약을 서버에 등록한다 (POST /api/reservations, REQ-001, D-03). */
    fun createReservation(serverUrl: String, sharedSecret: String, scheduledAtIso: String): CreateReservationResult {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim(); val cleanScheduledAt = scheduledAtIso.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return CreateReservationResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        if (cleanScheduledAt.isEmpty()) return CreateReservationResult.Failure(400, "예약 일시가 지정되지 않았습니다.")
        return try {
            val (code, body) = postJson("$cleanUrl/api/reservations", cleanSecret, JSONObject().apply { put("scheduled_at", cleanScheduledAt) })
            when (code) {
                HttpURLConnection.HTTP_CREATED, HttpURLConnection.HTTP_OK -> JSONObject(body).let {
                    CreateReservationResult.Success(it.optString("id", ""), it.optString("scheduled_at", it.optString("reserved_at", cleanScheduledAt)), it.optString("status", "예정"))
                }
                HttpURLConnection.HTTP_BAD_REQUEST -> CreateReservationResult.Failure(400, extractErrorMsg(body, "과거 시각 또는 잘못된 형식입니다: HTTP 400"))
                HttpURLConnection.HTTP_UNAUTHORIZED -> CreateReservationResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> CreateReservationResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            CreateReservationResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "예약 생성 중 오류: ${e.message}")
        }
    }

    /** 1회성 청소 예약 일시를 수정한다 (PUT /api/reservations/{id}, REQ-003, LB-018). */
    fun updateReservation(serverUrl: String, sharedSecret: String, reservationId: String, newScheduledAt: String): UpdateReservationResult {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim(); val cleanId = reservationId.trim(); val cleanScheduledAt = newScheduledAt.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return UpdateReservationResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        if (cleanId.isEmpty() || cleanScheduledAt.isEmpty()) return UpdateReservationResult.Failure(400, "예약 ID와 새 예약 일시가 필요합니다.")
        return try {
            val (code, body) = putJson("$cleanUrl/api/reservations/$cleanId", cleanSecret, JSONObject().apply { put("scheduled_at", cleanScheduledAt) })
            when (code) {
                HttpURLConnection.HTTP_OK -> JSONObject(body).let { UpdateReservationResult.Success(it.optString("id", cleanId), it.optString("scheduled_at", it.optString("reserved_at", cleanScheduledAt)), it.optString("status", "예정")) }
                HttpURLConnection.HTTP_BAD_REQUEST -> UpdateReservationResult.Failure(400, extractErrorMsg(body, "과거 시각 또는 수정할 수 없는 예약입니다."))
                HttpURLConnection.HTTP_UNAUTHORIZED -> UpdateReservationResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                HttpURLConnection.HTTP_NOT_FOUND -> UpdateReservationResult.Failure(404, extractErrorMsg(body, "해당 예약을 찾을 수 없습니다."))
                else -> UpdateReservationResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            UpdateReservationResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "예약 수정 중 오류: ${e.message}")
        }
    }

    /** 1회성 청소 예약을 취소한다 (POST /api/reservations/{id}/cancel, REQ-004, LB-019). */
    fun cancelReservation(serverUrl: String, sharedSecret: String, reservationId: String): CancelReservationResult {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim(); val cleanId = reservationId.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return CancelReservationResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        if (cleanId.isEmpty()) return CancelReservationResult.Failure(400, "예약 ID가 지정되지 않았습니다.")
        return try {
            val (code, body) = postJson("$cleanUrl/api/reservations/$cleanId/cancel", cleanSecret, JSONObject())
            when (code) {
                HttpURLConnection.HTTP_OK -> JSONObject(body).let { CancelReservationResult.Success(it.optString("id", cleanId), it.optString("status", "취소")) }
                HttpURLConnection.HTTP_BAD_REQUEST -> CancelReservationResult.Failure(400, extractErrorMsg(body, "이미 실행되었거나 취소할 수 없는 예약입니다."))
                HttpURLConnection.HTTP_UNAUTHORIZED -> CancelReservationResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                HttpURLConnection.HTTP_NOT_FOUND -> CancelReservationResult.Failure(404, extractErrorMsg(body, "해당 예약을 찾을 수 없습니다."))
                else -> CancelReservationResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            CancelReservationResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "예약 취소 중 오류: ${e.message}")
        }
    }

    /** 쉬는 기간을 서버에 등록한다 (POST /api/off-periods, REQ-011). */
    fun createOffPeriod(serverUrl: String, sharedSecret: String, startAtIso: String, endAtIso: String): CreateOffPeriodResult {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim(); val cleanStart = startAtIso.trim(); val cleanEnd = endAtIso.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return CreateOffPeriodResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        if (cleanStart.isEmpty() || cleanEnd.isEmpty()) return CreateOffPeriodResult.Failure(400, "시작 일시와 종료 일시가 필요합니다.")
        return try {
            val (code, body) = postJson("$cleanUrl/api/off-periods", cleanSecret, JSONObject().apply { put("start_at", cleanStart); put("end_at", cleanEnd) })
            when (code) {
                HttpURLConnection.HTTP_CREATED, HttpURLConnection.HTTP_OK -> JSONObject(body).let { CreateOffPeriodResult.Success(it.optString("id", ""), it.optString("start_at", cleanStart), it.optString("end_at", cleanEnd)) }
                HttpURLConnection.HTTP_BAD_REQUEST -> CreateOffPeriodResult.Failure(400, extractErrorMsg(body, "종료 일시는 시작 일시보다 뒤여야 합니다."))
                HttpURLConnection.HTTP_UNAUTHORIZED -> CreateOffPeriodResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> CreateOffPeriodResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            CreateOffPeriodResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "쉬는 기간 등록 중 오류: ${e.message}")
        }
    }

    /** 쉬는 기간을 삭제한다 (DELETE /api/off-periods/{id}, REQ-013, LB-022). */
    fun deleteOffPeriod(serverUrl: String, sharedSecret: String, offPeriodId: String): DeleteOffPeriodResult {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim(); val cleanId = offPeriodId.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return DeleteOffPeriodResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        if (cleanId.isEmpty()) return DeleteOffPeriodResult.Failure(400, "쉬는 기간 ID가 필요합니다.")
        return try {
            val (code, body) = deleteHttp("$cleanUrl/api/off-periods/$cleanId", cleanSecret)
            when (code) {
                HttpURLConnection.HTTP_OK -> JSONObject(body).let { DeleteOffPeriodResult.Success(it.optString("id", cleanId)) }
                HttpURLConnection.HTTP_NOT_FOUND -> DeleteOffPeriodResult.Failure(404, extractErrorMsg(body, "해당 쉬는 기간을 찾을 수 없습니다."))
                HttpURLConnection.HTTP_UNAUTHORIZED -> DeleteOffPeriodResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> DeleteOffPeriodResult.Failure(code, extractErrorMsg(body, "서버 응답 오류 (HTTP $code)"))
            }
        } catch (e: Exception) {
            DeleteOffPeriodResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "쉬는 기간 삭제 중 오류: ${e.message}")
        }
    }

    /** 예약 목록을 서버에서 조회한다 (GET /api/reservations, REQ-002). */
    fun fetchReservations(serverUrl: String, sharedSecret: String): FetchReservationsResult {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return FetchReservationsResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        return try {
            val (code, body) = getJson("$cleanUrl/api/reservations", cleanSecret)
            when (code) {
                HttpURLConnection.HTTP_OK -> (JSONObject(body).optJSONArray("reservations") ?: JSONArray()).let { arr ->
                    FetchReservationsResult.Success((0 until arr.length()).map { ReservationItem.fromJson(arr.getJSONObject(it)) })
                }
                HttpURLConnection.HTTP_UNAUTHORIZED -> FetchReservationsResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> FetchReservationsResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            FetchReservationsResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "예약 목록 조회 중 오류: ${e.message}")
        }
    }

    /** 쉬는 기간 목록을 서버에서 조회한다 (GET /api/off-periods, REQ-012, REQ-010, D-09). */
    fun fetchOffPeriods(serverUrl: String, sharedSecret: String): FetchOffPeriodsResult {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return FetchOffPeriodsResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        return try {
            val (code, body) = getJson("$cleanUrl/api/off-periods", cleanSecret)
            when (code) {
                HttpURLConnection.HTTP_OK -> (JSONObject(body).optJSONArray("off_periods") ?: JSONArray()).let { arr ->
                    FetchOffPeriodsResult.Success((0 until arr.length()).map { OffPeriodItem.fromJson(arr.getJSONObject(it)) })
                }
                HttpURLConnection.HTTP_UNAUTHORIZED -> FetchOffPeriodsResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> FetchOffPeriodsResult.Failure(code, "서버 응답 오류 (HTTP $code)")
            }
        } catch (e: Exception) {
            FetchOffPeriodsResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "쉬는 기간 목록 조회 중 오류: ${e.message}")
        }
    }

    /** 푸시 토큰을 서버에 등록한다 (POST /api/notification-targets, REQ-022, D-12). */
    fun registerNotificationTarget(serverUrl: String, sharedSecret: String, deviceId: String, pushToken: String): RegisterNotificationResult {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim(); val cleanDevice = deviceId.trim(); val cleanToken = pushToken.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return RegisterNotificationResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        if (cleanDevice.isEmpty() || cleanToken.isEmpty()) return RegisterNotificationResult.Failure(400, "기기 식별자와 푸시 토큰이 필요합니다.")
        return try {
            val (code, body) = postJson("$cleanUrl/api/notification-targets", cleanSecret, JSONObject().apply { put("device_id", cleanDevice); put("push_token", cleanToken) })
            when (code) {
                HttpURLConnection.HTTP_CREATED, HttpURLConnection.HTTP_OK -> JSONObject(body).let {
                    RegisterNotificationResult.Success(it.optString("device_id", cleanDevice), it.optString("push_token", cleanToken))
                }
                HttpURLConnection.HTTP_UNAUTHORIZED -> RegisterNotificationResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> RegisterNotificationResult.Failure(code, extractErrorMsg(body, "서버 응답 오류 (HTTP $code)"))
            }
        } catch (e: Exception) {
            RegisterNotificationResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "기기 등록 중 오류: ${e.message}")
        }
    }

    /** 시험 알림을 등록된 모든 기기(두 대: S21+, 폴드8)에 발송한다 (POST /api/notify/test, D-12, done_when). */
    fun sendTestNotification(serverUrl: String, sharedSecret: String, title: String = "청소기 시험 알림", body: String = "시험 푸시 알림입니다."): SendNotificationResult {
        val cleanUrl = normalizeUrl(serverUrl); val cleanSecret = sharedSecret.trim()
        if (cleanUrl.isEmpty() || cleanSecret.isEmpty()) return SendNotificationResult.Failure(400, "서버 주소와 공유 비밀키가 필요합니다.")
        return try {
            val (code, responseBody) = postJson("$cleanUrl/api/notify/test", cleanSecret, JSONObject().apply { put("title", title); put("body", body) })
            when (code) {
                HttpURLConnection.HTTP_OK -> JSONObject(responseBody).let {
                    SendNotificationResult.Success(it.optInt("total_targets", 0), it.optInt("success_count", 0))
                }
                HttpURLConnection.HTTP_UNAUTHORIZED -> SendNotificationResult.Failure(401, "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)")
                else -> SendNotificationResult.Failure(code, extractErrorMsg(responseBody, "서버 응답 오류 (HTTP $code)"))
            }
        } catch (e: Exception) {
            SendNotificationResult.Failure(0, if (e is IOException) "서버에 연결할 수 없습니다: ${e.message}" else "시험 알림 발송 중 오류: ${e.message}")
        }
    }

    private fun readStream(inputStream: InputStream): String =
        BufferedReader(InputStreamReader(inputStream, Charsets.UTF_8)).use { it.readText() }

    private sealed interface HealthCheckResult {
        data class Success(val serviceName: String, val serverTimeKst: String) : HealthCheckResult
        data class Failure(val message: String) : HealthCheckResult
    }
    private sealed interface AuthCheckResult {
        data class Success(val reservationCount: Int) : AuthCheckResult; data object Unauthorized : AuthCheckResult
        data class Failure(val message: String) : AuthCheckResult
    }
}
