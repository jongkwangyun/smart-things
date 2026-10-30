package com.smartthings.cleaner

import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.card.MaterialCardView
import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import com.smartthings.cleaner.network.ServerClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 앱 진입 메인 액티비티.
 *
 * SPEC:
 * - done_when: APK를 갤럭시 S21+에 설치하고 서버 주소·비밀키를 입력하면 첫 화면에 '연결됨'이 표시된다
 * - D-01: APK 직접 설치 방식
 * - D-09: 두 사용자 모두 같은 비밀키를 쓴다
 * - REQ-024: 비인가 접근 차단 (비밀키 검증)
 * - REQ-025: 서버 연결 불가 경고
 * - REQ-028: 갤럭시 S21+ 지원
 * - REQ-029: 갤럭시 폴드8 (접힘/펼침 반응형) 지원
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        initViews()
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

        btnConnect.setOnClickListener {
            performConnectionCheck()
        }
    }

    /**
     * 앱 실행 시 서버 연결 확인 (D-13: 백그라운드 감시 없음, 앱 실행 시 확인만).
     */
    private fun checkConnectionOnLaunch() {
        loadSavedCredentials()
    }

    private fun loadSavedCredentials() {
        val savedUrl = ServerClient.getSavedServerUrl(this)
        val savedSecret = ServerClient.getSavedSharedSecret(this)

        if (savedUrl.isNotEmpty()) {
            editServerUrl.setText(savedUrl)
        }
        if (savedSecret.isNotEmpty()) {
            editSharedSecret.setText(savedSecret)
        }

        // 저장된 자격 증명이 있으면 앱 실행 시 즉시 연결 상태 확인 (D-13)
        if (savedUrl.isNotEmpty() && savedSecret.isNotEmpty()) {
            performConnectionCheck()
        }
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
        cardConnectionStatus.setCardBackgroundColor(
            ContextCompat.getColor(this, R.color.status_connecting_bg)
        )
        cardConnectionStatus.strokeColor = ContextCompat.getColor(this, R.color.status_connecting)

        textStatusDetail.visibility = View.VISIBLE
        textStatusDetail.text = getString(R.string.status_connecting)
        cardServerInfo.visibility = View.GONE
    }

    private fun renderConnectionState(
        state: ConnectionState,
        serverUrl: String,
        sharedSecret: String
    ) {
        btnConnect.isEnabled = true
        btnConnect.text = getString(R.string.btn_connect)
        progressBar.visibility = View.GONE

        when (state) {
            is ConnectionState.Connected -> {
                cardServerWarning.visibility = View.GONE

                // 자격 증명 로컬 저장
                ServerClient.saveCredentials(this, serverUrl, sharedSecret)

                // 상태 표시: '연결됨' (done_when 기준)
                textConnectionStatus.text = getString(R.string.status_connected)
                textConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.status_connected))
                cardConnectionStatus.setCardBackgroundColor(
                    ContextCompat.getColor(this, R.color.status_connected_bg)
                )
                cardConnectionStatus.strokeColor = ContextCompat.getColor(this, R.color.status_connected)

                textStatusDetail.visibility = View.VISIBLE
                textStatusDetail.text = "서버(${state.serverUrl})와 정상적으로 통신 중입니다."

                // 서버 정보 카드 표시
                cardServerInfo.visibility = View.VISIBLE
                textServiceName.text = getString(R.string.server_info_service, state.serviceName)
                textServerTime.text = getString(R.string.server_info_time, state.serverTimeKst)
                textReservationCount.text =
                    getString(R.string.server_info_reservations, state.reservationCount)
            }
            is ConnectionState.Failed -> {
                cardServerInfo.visibility = View.GONE

                if (state.error == ConnectionError.UNREACHABLE) {
                    // 서버 연결 불가 경고 표시 (REQ-025, D-13)
                    cardServerWarning.visibility = View.VISIBLE
                    textServerWarningTitle.text = getString(R.string.server_unreachable_warning)
                    textServerWarningMessage.text = state.message

                    textConnectionStatus.text = getString(R.string.server_unreachable_warning)
                    textConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.status_error))
                    cardConnectionStatus.setCardBackgroundColor(
                        ContextCompat.getColor(this, R.color.status_error_bg)
                    )
                    cardConnectionStatus.strokeColor = ContextCompat.getColor(this, R.color.status_error)

                    textStatusDetail.visibility = View.VISIBLE
                    textStatusDetail.text = state.message
                } else {
                    cardServerWarning.visibility = View.GONE

                    textConnectionStatus.text = getString(R.string.status_failed)
                    textConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.status_error))
                    cardConnectionStatus.setCardBackgroundColor(
                        ContextCompat.getColor(this, R.color.status_error_bg)
                    )
                    cardConnectionStatus.strokeColor = ContextCompat.getColor(this, R.color.status_error)

                    textStatusDetail.visibility = View.VISIBLE
                    textStatusDetail.text = state.message
                }
            }
            is ConnectionState.Disconnected -> {
                cardServerWarning.visibility = View.GONE

                textConnectionStatus.text = getString(R.string.status_disconnected)
                textConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.status_disconnected))
                cardConnectionStatus.setCardBackgroundColor(
                    ContextCompat.getColor(this, R.color.status_disconnected_bg)
                )
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
