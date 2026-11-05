package com.smartthings.cleaner

import android.content.Intent
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
 * 서버 연결 화면 (LB-047).
 * 저장된 서버 정보가 있으면 이 화면(연결 폼)을 거치지 않고 바로 [HomeActivity]를 연다(LB-042 자동 연결은 홈에서 수행).
 * 주소와 비밀키는 연결 성공 시에만 저장한다(D-09). 설정의 "서버 주소 변경"은 [EXTRA_CHANGE_SERVER]로 이 화면을 다시 연다.
 * SPEC: D-01, D-09, D-13, REQ-024, REQ-025.
 */
class MainActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_CHANGE_SERVER = "EXTRA_CHANGE_SERVER"
    }

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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val changeServer = intent.getBooleanExtra(EXTRA_CHANGE_SERVER, false)
        if (!changeServer && ServerClient.getSavedServerUrl(this).isNotEmpty() &&
            ServerClient.getSavedSharedSecret(this).isNotEmpty()
        ) {
            startActivity(Intent(this, HomeActivity::class.java))
            finish()
            return
        }
        setContentView(R.layout.activity_main)
        initViews()
        if (changeServer) {
            editServerUrl.setText(ServerClient.getSavedServerUrl(this))
        }
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
        btnConnect.setOnClickListener { performConnectionCheck() }
    }

    private fun performConnectionCheck() {
        val serverUrl = editServerUrl.text.toString().trim()
        val sharedSecret = editSharedSecret.text.toString().trim()
        btnConnect.isEnabled = false
        btnConnect.text = getString(R.string.btn_connecting)
        progressBar.visibility = View.VISIBLE
        cardServerWarning.visibility = View.GONE
        cardConnectionStatus.visibility = View.GONE

        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.testConnection(serverUrl, sharedSecret)
            withContext(Dispatchers.Main) { handleResult(result, serverUrl, sharedSecret) }
        }
    }

    private fun handleResult(state: ConnectionState, serverUrl: String, sharedSecret: String) {
        btnConnect.isEnabled = true
        btnConnect.text = getString(R.string.btn_connect)
        progressBar.visibility = View.GONE
        if (state is ConnectionState.Connected) {
            // 최초 연결 성공 시에만 저장한다. 실패하면 아무것도 저장하지 않는다.
            ServerClient.saveCredentials(this, serverUrl, sharedSecret)
            startActivity(
                Intent(this, HomeActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
            )
            finish()
            return
        }
        if (state is ConnectionState.Failed) showFailure(state)
    }

    private fun showFailure(state: ConnectionState.Failed) {
        val isUnreachable = state.error == ConnectionError.UNREACHABLE
        cardServerWarning.visibility = if (isUnreachable) View.VISIBLE else View.GONE
        if (isUnreachable) {
            textServerWarningTitle.text = getString(R.string.server_unreachable_warning)
            textServerWarningMessage.text = state.message
        }
        cardConnectionStatus.visibility = View.VISIBLE
        textConnectionStatus.text = getString(R.string.status_failed)
        textConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.status_error))
        cardConnectionStatus.setCardBackgroundColor(ContextCompat.getColor(this, R.color.status_error_bg))
        cardConnectionStatus.strokeColor = ContextCompat.getColor(this, R.color.status_error)
        textStatusDetail.visibility = View.VISIBLE
        textStatusDetail.text = state.message
    }
}
