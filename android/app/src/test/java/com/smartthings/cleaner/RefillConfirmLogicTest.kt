package com.smartthings.cleaner

import com.smartthings.cleaner.model.WaterTrackerResult
import com.smartthings.cleaner.network.ServerClient
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.net.ServerSocket
import java.net.Socket

/**
 * 물 채움 확인 대화상자 보정 계산식 및 ServerClient refillWater 직렬화 검증 (REQ-019, REQ-021, LB-060).
 */
class RefillConfirmLogicTest {

    private fun readHttpRequest(socket: Socket): Pair<String, String> {
        val input = socket.getInputStream()
        val headerBytes = ByteArrayOutputStream()
        var p1 = -1; var p2 = -1; var p3 = -1
        while (true) {
            val b = input.read()
            if (b == -1) break
            headerBytes.write(b)
            if (p3 == '\r'.code && p2 == '\n'.code && p1 == '\r'.code && b == '\n'.code) break
            p3 = p2; p2 = p1; p1 = b
        }
        val headers = headerBytes.toString(Charsets.UTF_8.name())
        var contentLength = 0
        for (line in headers.lines()) {
            if (line.lowercase().startsWith("content-length:")) {
                contentLength = line.substring(15).trim().toIntOrNull() ?: 0
            }
        }
        val bodyBytes = ByteArray(contentLength)
        var readTotal = 0
        while (readTotal < contentLength) {
            val count = input.read(bodyBytes, readTotal, contentLength - readTotal)
            if (count < 0) break
            readTotal += count
        }
        return Pair(headers, String(bodyBytes, Charsets.UTF_8))
    }

    @Test
    fun testCalculateCalibratedThreshold_formulaMax1KMinus1() {
        // max(1, K - 1) 수식 검증 (REQ-021, docs/device-capabilities.md 섹션 10)
        assertEquals(1, RefillConfirmDialog.calculateCalibratedThreshold(0))
        assertEquals(1, RefillConfirmDialog.calculateCalibratedThreshold(1))
        assertEquals(1, RefillConfirmDialog.calculateCalibratedThreshold(2))
        assertEquals(2, RefillConfirmDialog.calculateCalibratedThreshold(3))
        assertEquals(4, RefillConfirmDialog.calculateCalibratedThreshold(5))
        assertEquals(6, RefillConfirmDialog.calculateCalibratedThreshold(7))
        assertEquals(9, RefillConfirmDialog.calculateCalibratedThreshold(10))
    }

    @Test
    fun testServerClientRefillWater_withCalibrateTrue() {
        var receivedPayload: String? = null
        val serverSocket = ServerSocket(0)
        val port = serverSocket.localPort

        val thread = Thread {
            try {
                val clientSocket = serverSocket.accept()
                val (_, body) = readHttpRequest(clientSocket)
                receivedPayload = body

                val responseJson = JSONObject().apply {
                    put("id", "default")
                    put("mop_clean_count", 0)
                    put("alert_threshold_n", 6)
                    put("last_refilled_at", "2026-11-12T15:00:00+09:00")
                }
                val respBytes = responseJson.toString().toByteArray(Charsets.UTF_8)
                val out = clientSocket.getOutputStream()
                out.write("HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=UTF-8\r\nContent-Length: ${respBytes.size}\r\nConnection: close\r\n\r\n".toByteArray(Charsets.UTF_8))
                out.write(respBytes)
                out.flush()
                clientSocket.close()
            } catch (_: Exception) {}
        }
        thread.start()

        val client = ServerClient(connectTimeoutMs = 2000, readTimeoutMs = 2000)
        val result = client.refillWater("http://127.0.0.1:$port", "secret", calibrateThreshold = true)
        thread.join(2000)
        serverSocket.close()

        assertTrue("결과 성공: $result", result is WaterTrackerResult.Success)
        val item = (result as WaterTrackerResult.Success).item
        assertEquals(0, item.mopCleanCount)
        assertEquals(6, item.alertThresholdN)

        val json = JSONObject(receivedPayload ?: "{}")
        assertEquals(true, json.optBoolean("calibrate_threshold"))
    }

    @Test
    fun testServerClientRefillWater_defaultCalibrateFalse() {
        var receivedPayload: String? = null
        val serverSocket = ServerSocket(0)
        val port = serverSocket.localPort

        val thread = Thread {
            try {
                val clientSocket = serverSocket.accept()
                val (_, body) = readHttpRequest(clientSocket)
                receivedPayload = body

                val responseJson = JSONObject().apply {
                    put("id", "default")
                    put("mop_clean_count", 0)
                    put("alert_threshold_n", 7)
                    put("last_refilled_at", "2026-11-12T15:00:00+09:00")
                }
                val respBytes = responseJson.toString().toByteArray(Charsets.UTF_8)
                val out = clientSocket.getOutputStream()
                out.write("HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=UTF-8\r\nContent-Length: ${respBytes.size}\r\nConnection: close\r\n\r\n".toByteArray(Charsets.UTF_8))
                out.write(respBytes)
                out.flush()
                clientSocket.close()
            } catch (_: Exception) {}
        }
        thread.start()

        val client = ServerClient(connectTimeoutMs = 2000, readTimeoutMs = 2000)
        val result = client.refillWater("http://127.0.0.1:$port", "secret")
        thread.join(2000)
        serverSocket.close()

        assertTrue("결과 성공: $result", result is WaterTrackerResult.Success)
        val item = (result as WaterTrackerResult.Success).item
        assertEquals(0, item.mopCleanCount)
        assertEquals(7, item.alertThresholdN)

        val json = JSONObject(receivedPayload ?: "{}")
        assertEquals(false, json.optBoolean("calibrate_threshold"))
    }
}
