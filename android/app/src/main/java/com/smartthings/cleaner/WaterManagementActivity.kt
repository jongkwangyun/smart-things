package com.smartthings.cleaner

import android.os.Bundle
import android.view.View
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
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
 * 물 관리 액티비티 (LB-038, REQ-019, D-06, D-14).
 *
 * Done When: 앱에서 '물 채움' 버튼을 누르면 화면의 '물 채운 뒤 청소 횟수'가 0으로 바뀌고 마지막 물 채움 일시가 갱신된다.
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

    var serverUrl: String = ""
    var sharedSecret: String = ""

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

        btnRefillWater.setOnClickListener { performRefillWater() }
        btnRefresh.setOnClickListener { loadWaterTracker() }
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

    fun renderWaterTracker(item: WaterTrackerItem) {
        textMopCleanCount.text = getString(R.string.text_count_times, item.mopCleanCount)
        textLastRefilledAt.text = item.formatLastRefilledDisplay()
        textAlertThreshold.text = getString(R.string.text_alert_threshold_format, item.alertThresholdN)
    }

    private fun setLoading(loading: Boolean, isRefilling: Boolean = false) {
        progressBar.visibility = if (loading) View.VISIBLE else View.GONE
        btnRefillWater.isEnabled = !loading
        btnRefresh.isEnabled = !loading
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
