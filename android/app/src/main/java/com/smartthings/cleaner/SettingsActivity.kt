package com.smartthings.cleaner

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.smartthings.cleaner.model.SendNotificationResult
import com.smartthings.cleaner.network.ServerClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 설정 화면 (LB-047). 홈에서 옮겨 온 시험 알림 발송과 서버 주소 변경을 제공한다.
 * 서버 주소 변경은 연결 화면([MainActivity])을 다시 열며, 연결에 성공해야만 새 정보가 저장된다.
 */
class SettingsActivity : AppCompatActivity() {

    private val serverClient = ServerClient()
    private lateinit var textMessage: TextView
    private lateinit var textCurrentServer: TextView
    private lateinit var btnTest: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        textMessage = findViewById(R.id.textSettingsMessage)
        textCurrentServer = findViewById(R.id.textCurrentServer)
        btnTest = findViewById(R.id.btnSendTestNotification)
        findViewById<View>(R.id.btnChangeServer).setOnClickListener {
            startActivity(Intent(this, MainActivity::class.java).putExtra(MainActivity.EXTRA_CHANGE_SERVER, true))
        }
        btnTest.setOnClickListener { sendTestNotification() }
    }

    override fun onResume() {
        super.onResume()
        textCurrentServer.text = ServerClient.getSavedServerUrl(this)
    }

    private fun sendTestNotification() {
        val serverUrl = ServerClient.getSavedServerUrl(this)
        val sharedSecret = ServerClient.getSavedSharedSecret(this)
        textMessage.visibility = View.VISIBLE
        if (serverUrl.isEmpty() || sharedSecret.isEmpty()) {
            textMessage.text = getString(R.string.msg_error_server_not_configured)
            return
        }
        btnTest.isEnabled = false
        textMessage.text = getString(R.string.msg_sending_test_notification)
        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.sendTestNotification(
                serverUrl,
                sharedSecret,
                "청소기 시험 알림",
                "시험 푸시 알림입니다. 갤럭시 S21+와 갤럭시 폴드8 두 대 모두 정상 수신되었습니다."
            )
            withContext(Dispatchers.Main) {
                btnTest.isEnabled = true
                textMessage.text = when (result) {
                    is SendNotificationResult.Success -> getString(R.string.msg_test_notification_success, result.successCount)
                    is SendNotificationResult.Failure -> getString(R.string.msg_test_notification_failed, result.message)
                }
            }
        }
    }
}
