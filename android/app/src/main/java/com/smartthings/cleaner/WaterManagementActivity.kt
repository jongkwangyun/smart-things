package com.smartthings.cleaner

import android.os.Bundle
import android.text.InputType
import android.view.View
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.smartthings.cleaner.model.WaterTrackerItem
import com.smartthings.cleaner.model.WaterTrackerResult
import com.smartthings.cleaner.network.ServerClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 물 관리 액티비티 (LB-038, LB-040, REQ-019, REQ-021, D-06, D-14).
 *
 * Done When (LB-040): 앱 설정에 기본 N값이 표시되고, 값을 바꾸면 다음 알림이 바뀐 값 기준으로 온다.
 * Must: 1 이상의 정수만 허용한다.
 */
class WaterManagementActivity : AppCompatActivity() {

    val serverClient = ServerClient()

    lateinit var textMopCleanCount: TextView
    lateinit var textLastRefilledAt: TextView
    lateinit var textAlertThreshold: TextView
    lateinit var textStatusMessage: TextView
    lateinit var progressBar: ProgressBar
    lateinit var btnRefillWater: MaterialButton
    lateinit var btnRefresh: MaterialButton
    lateinit var btnChangeThreshold: MaterialButton

    var serverUrl: String = ""
    var sharedSecret: String = ""
    var currentThresholdN: Int = 7

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_water_management)

        serverUrl = intent.getStringExtra("EXTRA_SERVER_URL") ?: ServerClient.getSavedServerUrl(this)
        sharedSecret = intent.getStringExtra("EXTRA_SHARED_SECRET") ?: ServerClient.getSavedSharedSecret(this)

        initViews()
    }

    override fun onResume() {
        super.onResume()
        loadWaterTracker()
    }

    private fun initViews() {
        textMopCleanCount = findViewById(R.id.textMopCleanCount)
        textLastRefilledAt = findViewById(R.id.textLastRefilledAt)
        textAlertThreshold = findViewById(R.id.textAlertThreshold)
        textStatusMessage = findViewById(R.id.textStatusMessage)
        progressBar = findViewById(R.id.progressBar)
        btnRefillWater = findViewById(R.id.btnRefillWater)
        btnRefresh = findViewById(R.id.btnRefresh)
        btnChangeThreshold = findViewById(R.id.btnChangeThreshold)

        // LB-053: 확인 대화상자에서 확인한 경우에만 물 채움을 기록한다.
        btnRefillWater.setOnClickListener { RefillConfirmDialog.show(this) { performRefillWater() } }
        btnRefresh.setOnClickListener { loadWaterTracker() }
        btnChangeThreshold.setOnClickListener { showChangeThresholdDialog() }
    }

    fun loadWaterTracker() {
        if (serverUrl.isEmpty() || sharedSecret.isEmpty()) {
            showStatus(getString(R.string.msg_error_server_not_configured), isError = true)
            return
        }
        setLoading(true)
        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.fetchWaterTracker(serverUrl, sharedSecret)
            withContext(Dispatchers.Main) {
                setLoading(false)
                when (result) {
                    is WaterTrackerResult.Success -> renderWaterTracker(result.item)
                    is WaterTrackerResult.Failure -> showStatus(result.message, isError = true)
                }
            }
        }
    }

    fun performRefillWater() {
        if (serverUrl.isEmpty() || sharedSecret.isEmpty()) {
            showStatus(getString(R.string.msg_error_server_not_configured), isError = true)
            return
        }
        setLoading(true, isRefilling = true)
        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.refillWater(serverUrl, sharedSecret)
            withContext(Dispatchers.Main) {
                setLoading(false)
                when (result) {
                    is WaterTrackerResult.Success -> {
                        renderWaterTracker(result.item)
                        Toast.makeText(this@WaterManagementActivity, getString(R.string.msg_confirm_water_refilled), Toast.LENGTH_SHORT).show()
                        showStatus(getString(R.string.msg_confirm_water_refilled), isError = false)
                    }
                    is WaterTrackerResult.Failure -> showStatus(result.message, isError = true)
                }
            }
        }
    }

    /** 알림 기준 횟수 N 변경 대화상자를 표시한다 (REQ-021, LB-040). */
    fun showChangeThresholdDialog() {
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            hint = getString(R.string.dialog_hint_threshold)
            setText(currentThresholdN.toString())
            setSelection(text.length)
        }
        val container = FrameLayout(this).apply {
            val pad = (20 * resources.displayMetrics.density).toInt()
            setPadding(pad, pad / 2, pad, 0)
            addView(input)
        }

        AlertDialog.Builder(this)
            .setTitle(R.string.dialog_title_change_threshold)
            .setMessage(R.string.dialog_msg_change_threshold)
            .setView(container)
            .setPositiveButton(R.string.btn_save) { _, _ ->
                val rawVal = input.text.toString().trim()
                val nVal = rawVal.toIntOrNull()
                // Must: 1 이상의 정수만 허용한다.
                if (nVal == null || nVal < 1) {
                    Toast.makeText(this, getString(R.string.msg_error_invalid_threshold), Toast.LENGTH_SHORT).show()
                    showStatus(getString(R.string.msg_error_invalid_threshold), isError = true)
                    return@setPositiveButton
                }
                performUpdateThreshold(nVal)
            }
            .setNegativeButton(R.string.btn_cancel, null)
            .show()
    }

    /** 서버에 변경된 알림 기준 횟수 N을 전송한다 (REQ-021, LB-040). */
    fun performUpdateThreshold(newN: Int) {
        if (serverUrl.isEmpty() || sharedSecret.isEmpty()) {
            showStatus(getString(R.string.msg_error_server_not_configured), isError = true)
            return
        }
        // Must: 1 이상의 정수만 허용한다.
        if (newN < 1) {
            showStatus(getString(R.string.msg_error_invalid_threshold), isError = true)
            return
        }
        setLoading(true)
        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.updateAlertThreshold(serverUrl, sharedSecret, newN)
            withContext(Dispatchers.Main) {
                setLoading(false)
                when (result) {
                    is WaterTrackerResult.Success -> {
                        renderWaterTracker(result.item)
                        val msg = getString(R.string.msg_success_threshold_updated, result.item.alertThresholdN)
                        Toast.makeText(this@WaterManagementActivity, msg, Toast.LENGTH_SHORT).show()
                        showStatus(msg, isError = false)
                    }
                    is WaterTrackerResult.Failure -> showStatus(result.message, isError = true)
                }
            }
        }
    }

    fun renderWaterTracker(item: WaterTrackerItem) {
        currentThresholdN = item.alertThresholdN
        textMopCleanCount.text = getString(R.string.text_count_times, item.mopCleanCount)
        textLastRefilledAt.text = item.formatLastRefilledDisplay()
        textAlertThreshold.text = getString(R.string.text_alert_threshold_format, item.alertThresholdN)
    }

    private fun setLoading(loading: Boolean, isRefilling: Boolean = false) {
        progressBar.visibility = if (loading) View.VISIBLE else View.GONE
        btnRefillWater.isEnabled = !loading
        btnRefresh.isEnabled = !loading
        btnChangeThreshold.isEnabled = !loading
        if (loading && isRefilling) {
            btnRefillWater.text = getString(R.string.btn_refilling_water)
        } else {
            btnRefillWater.text = getString(R.string.btn_refill_water)
        }
    }

    private fun showStatus(msg: String, isError: Boolean) {
        textStatusMessage.visibility = View.VISIBLE
        textStatusMessage.text = msg
        val colorRes = if (isError) R.color.status_error else R.color.primary
        textStatusMessage.setTextColor(getColor(colorRes))
    }
}
