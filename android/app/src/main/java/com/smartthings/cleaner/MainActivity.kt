package com.smartthings.cleaner

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.card.MaterialCardView
import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import com.smartthings.cleaner.model.SendNotificationResult
import com.smartthings.cleaner.network.ServerClient
import com.smartthings.cleaner.notification.AppFirebaseMessagingService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 앱 진입 메인 액티비티.
 * SPEC: D-01, D-09, D-12, D-13, REQ-022, REQ-024, REQ-025, REQ-028, REQ-029.
 */
class MainActivity : AppCompatActivity() {

    private val serverClient = ServerClient()

    private lateinit var editServerUrl: EditText
    private lateinit var editSharedSecret: EditText
    private lateinit var btnConnect: Button
    private lateinit var progressBar: ProgressBar

    private lateinit var cardConnectionStatus: MaterialCardView
    private lateinit var textConnectionStatus: TextView
    private lateinit var textStatusDetail: TextView

    private lateinit var cardServerWarning: MaterialCardView
    private lateinit var textServerWarningTitle: TextView
    private lateinit var textServerWarningMessage: TextView

    private lateinit var cardServerInfo: MaterialCardView
    private lateinit var textServiceName: TextView
    private lateinit var textServerTime: TextView
    private lateinit var textReservationCount: TextView
    private lateinit var btnCreateReservation: Button
    private lateinit var btnReservationList: Button
    private lateinit var btnCreateOffPeriod: Button
    private lateinit var btnOffPeriodList: Button
    private lateinit var btnWaterManagement: Button
    private lateinit var btnSendTestNotification: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        initViews()
        setupNotifications()
        checkConnectionOnLaunch()
    }

    private fun initViews() {
        editServerUrl = findViewById(R.id.editServerUrl)
        editSharedSecret = findViewById(R.id.editSharedSecret)
        btnConnect = findViewById(R.id.btnConnect)
        progressBar = findViewById(R.id.progressBar)

        cardServerWarning = findViewById(R.id.cardServerWarning)
        textServerWarningTitle = findViewById(R.id.textServerWarningTitle)
        textServerWarningMessage = findViewById(R.id.textServerWarningMessage)

        cardConnectionStatus = findViewById(R.id.cardConnectionStatus)
        textConnectionStatus = findViewById(R.id.textConnectionStatus)
        textStatusDetail = findViewById(R.id.textStatusDetail)

        cardServerInfo = findViewById(R.id.cardServerInfo)
        textServiceName = findViewById(R.id.textServiceName)
        textServerTime = findViewById(R.id.textServerTime)
        textReservationCount = findViewById(R.id.textReservationCount)
        btnCreateReservation = findViewById(R.id.btnCreateReservation)
        btnReservationList = findViewById(R.id.btnReservationList)
        btnCreateOffPeriod = findViewById(R.id.btnCreateOffPeriod)
        btnOffPeriodList = findViewById(R.id.btnOffPeriodList)
        btnWaterManagement = findViewById(R.id.btnWaterManagement)
        btnSendTestNotification = findViewById(R.id.btnSendTestNotification)

        btnConnect.setOnClickListener { performConnectionCheck() }
        btnCreateReservation.setOnClickListener { navigateTo(CreateReservationActivity::class.java) }
        btnReservationList.setOnClickListener { navigateTo(ReservationListActivity::class.java) }
        btnCreateOffPeriod.setOnClickListener { navigateTo(CreateOffPeriodActivity::class.java) }
        btnOffPeriodList.setOnClickListener { navigateTo(OffPeriodListActivity::class.java) }
        btnWaterManagement.setOnClickListener { navigateTo(WaterManagementActivity::class.java) }
        btnSendTestNotification.setOnClickListener { performSendTestNotification() }
    }

    private fun navigateTo(clazz: Class<*>) {
        val intent = Intent(this, clazz).apply {
            putExtra("EXTRA_SERVER_URL", ServerClient.getSavedServerUrl(this@MainActivity))
            putExtra("EXTRA_SHARED_SECRET", ServerClient.getSavedSharedSecret(this@MainActivity))
        }
        startActivity(intent)
    }

    private fun setupNotifications() {
        AppFirebaseMessagingService.createNotificationChannel(this)
        if (Build.VERSION.SDK_INT >= 33) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 101)
            }
        }
    }

    private fun performSendTestNotification() {
        val serverUrl = ServerClient.getSavedServerUrl(this)
        val sharedSecret = ServerClient.getSavedSharedSecret(this)
        if (serverUrl.isEmpty() || sharedSecret.isEmpty()) {
            textStatusDetail.visibility = View.VISIBLE
            textStatusDetail.text = getString(R.string.msg_error_server_not_configured)
            return
        }
        btnSendTestNotification.isEnabled = false
        textStatusDetail.visibility = View.VISIBLE
        textStatusDetail.text = getString(R.string.msg_sending_test_notification)

        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.sendTestNotification(
                serverUrl,
                sharedSecret,
                "청소기 시험 알림",
                "시험 푸시 알림입니다. 갤럭시 S21+와 갤럭시 폴드8 두 대 모두 정상 수신되었습니다."
            )
            withContext(Dispatchers.Main) {
                btnSendTestNotification.isEnabled = true
                when (result) {
                    is SendNotificationResult.Success -> {
                        textStatusDetail.text = getString(R.string.msg_test_notification_success, result.successCount)
                    }
                    is SendNotificationResult.Failure -> {
                        textStatusDetail.text = getString(R.string.msg_test_notification_failed, result.message)
                    }
                }
            }
        }
    }

    /** 앱 실행 시 서버 연결 확인 (D-13: 백그라운드 감시 없음, 앱 실행 시 확인만). */
    private fun checkConnectionOnLaunch() {
        val savedUrl = ServerClient.getSavedServerUrl(this)
        val savedSecret = ServerClient.getSavedSharedSecret(this)
        if (savedUrl.isNotEmpty()) editServerUrl.setText(savedUrl)
        if (savedSecret.isNotEmpty()) editSharedSecret.setText(savedSecret)
        if (savedUrl.isNotEmpty() && savedSecret.isNotEmpty()) performConnectionCheck()
    }

    private fun performConnectionCheck() {
        val serverUrl = editServerUrl.text.toString().trim()
        val sharedSecret = editSharedSecret.text.toString().trim()
        setUiConnecting()

        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.testConnection(serverUrl, sharedSecret)
            withContext(Dispatchers.Main) {
                renderConnectionState(result, serverUrl, sharedSecret)
            }
        }
    }

    private fun setUiConnecting() {
        btnConnect.isEnabled = false
        btnConnect.text = getString(R.string.btn_connecting)
        progressBar.visibility = View.VISIBLE
        cardServerWarning.visibility = View.GONE
        textConnectionStatus.text = getString(R.string.status_connecting)
        textConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.status_connecting))
        cardConnectionStatus.setCardBackgroundColor(ContextCompat.getColor(this, R.color.status_connecting_bg))
        cardConnectionStatus.strokeColor = ContextCompat.getColor(this, R.color.status_connecting)
        textStatusDetail.visibility = View.VISIBLE
        textStatusDetail.text = getString(R.string.status_connecting)
        cardServerInfo.visibility = View.GONE
    }

    private fun renderConnectionState(state: ConnectionState, serverUrl: String, sharedSecret: String) {
        btnConnect.isEnabled = true
        btnConnect.text = getString(R.string.btn_connect)
        progressBar.visibility = View.GONE

        when (state) {
            is ConnectionState.Connected -> {
                cardServerWarning.visibility = View.GONE
                ServerClient.saveCredentials(this, serverUrl, sharedSecret)
                textConnectionStatus.text = getString(R.string.status_connected)
                textConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.status_connected))
                cardConnectionStatus.setCardBackgroundColor(ContextCompat.getColor(this, R.color.status_connected_bg))
                cardConnectionStatus.strokeColor = ContextCompat.getColor(this, R.color.status_connected)
                textStatusDetail.visibility = View.VISIBLE
                textStatusDetail.text = "서버(${state.serverUrl})와 정상적으로 통신 중입니다."
                cardServerInfo.visibility = View.VISIBLE
                textServiceName.text = getString(R.string.server_info_service, state.serviceName)
                textServerTime.text = getString(R.string.server_info_time, state.serverTimeKst)
                textReservationCount.text = getString(R.string.server_info_reservations, state.reservationCount)
            }
            is ConnectionState.Failed -> {
                cardServerInfo.visibility = View.GONE
                val isUnreachable = state.error == ConnectionError.UNREACHABLE
                cardServerWarning.visibility = if (isUnreachable) View.VISIBLE else View.GONE
                if (isUnreachable) {
                    textServerWarningTitle.text = getString(R.string.server_unreachable_warning)
                    textServerWarningMessage.text = state.message
                    textConnectionStatus.text = getString(R.string.server_unreachable_warning)
                } else {
                    textConnectionStatus.text = getString(R.string.status_failed)
                }
                textConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.status_error))
                cardConnectionStatus.setCardBackgroundColor(ContextCompat.getColor(this, R.color.status_error_bg))
                cardConnectionStatus.strokeColor = ContextCompat.getColor(this, R.color.status_error)
                textStatusDetail.visibility = View.VISIBLE
                textStatusDetail.text = state.message
            }
            is ConnectionState.Disconnected -> {
                cardServerWarning.visibility = View.GONE
                textConnectionStatus.text = getString(R.string.status_disconnected)
                textConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.status_disconnected))
                cardConnectionStatus.setCardBackgroundColor(ContextCompat.getColor(this, R.color.status_disconnected_bg))
                cardConnectionStatus.strokeColor = ContextCompat.getColor(this, R.color.border_color)
                textStatusDetail.visibility = View.GONE
                cardServerInfo.visibility = View.GONE
            }
            is ConnectionState.Connecting -> {
                cardServerWarning.visibility = View.GONE
                setUiConnecting()
            }
        }
    }
}
