package com.smartthings.cleaner

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.smartthings.cleaner.model.ConnectionError
import com.smartthings.cleaner.model.ConnectionState
import com.smartthings.cleaner.model.FetchReservationsResult
import com.smartthings.cleaner.model.WaterTrackerResult
import com.smartthings.cleaner.network.ServerClient
import com.smartthings.cleaner.notification.AppFirebaseMessagingService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.ZonedDateTime

/**
 * 홈 화면 (LB-047). 연결 상태 한 줄, 다음 예약 카드(예약 추가 주 버튼), 예약 목록/쉬는 기간/물 관리 타일.
 * 저장된 서버 정보로 열릴 때마다 서버에 연결을 확인한다(LB-042 자동 연결, D-13: 앱 실행 시 확인만).
 * 다음 예약·건수·물 횟수는 기존 API(fetchReservations / fetchWaterTracker)로 가져와 [HomeLogic]으로 계산한다(LB-052).
 * 각 영역은 독립적으로 성공/실패를 표시한다(부분 실패 허용).
 */
class HomeActivity : AppCompatActivity() {

    private val serverClient = ServerClient()
    private var loadJob: Job? = null

    private lateinit var textConnectionLine: TextView
    private lateinit var cardServerWarning: View
    private lateinit var textServerWarningMessage: TextView
    private lateinit var textNextReservation: TextView
    private lateinit var textReservationCounts: TextView
    private lateinit var textTileReservations: TextView
    private lateinit var textTileWater: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_home)
        textConnectionLine = findViewById(R.id.textConnectionLine)
        cardServerWarning = findViewById(R.id.cardServerWarning)
        textServerWarningMessage = findViewById(R.id.textServerWarningMessage)
        textNextReservation = findViewById(R.id.textNextReservation)
        textReservationCounts = findViewById(R.id.textReservationCounts)
        textTileReservations = findViewById(R.id.textTileReservations)
        textTileWater = findViewById(R.id.textTileWater)

        findViewById<View>(R.id.btnSettings).setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        findViewById<View>(R.id.btnCreateReservation).setOnClickListener { navigateTo(CreateReservationActivity::class.java) }
        findViewById<View>(R.id.tileReservationList).setOnClickListener { navigateTo(ReservationListActivity::class.java) }
        findViewById<View>(R.id.tileOffPeriod).setOnClickListener { navigateTo(OffPeriodListActivity::class.java) }
        findViewById<View>(R.id.tileWater).setOnClickListener { navigateTo(WaterManagementActivity::class.java) }
        // 물 채움은 0회 초기화(되돌릴 수 없음)이므로 반드시 확인 대화상자를 거친다(LB-053).
        findViewById<View>(R.id.btnRefillWater).setOnClickListener {
            RefillConfirmDialog.show(this) { performRefill() }
        }
        setupNotifications()
    }

    override fun onResume() {
        super.onResume()
        loadHome()
    }

    private fun navigateTo(clazz: Class<*>) {
        startActivity(Intent(this, clazz).apply {
            putExtra("EXTRA_SERVER_URL", ServerClient.getSavedServerUrl(this@HomeActivity))
            putExtra("EXTRA_SHARED_SECRET", ServerClient.getSavedSharedSecret(this@HomeActivity))
        })
    }

    private fun setupNotifications() {
        AppFirebaseMessagingService.createNotificationChannel(this)
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 101)
        }
    }

    private fun loadHome() {
        val url = ServerClient.getSavedServerUrl(this)
        val secret = ServerClient.getSavedSharedSecret(this)
        loadJob?.cancel()
        textConnectionLine.text = getString(R.string.home_conn_connecting)
        textConnectionLine.setTextColor(ContextCompat.getColor(this, R.color.status_connecting))
        loadJob = lifecycleScope.launch {
            val (conn, reservations, water) = withContext(Dispatchers.IO) {
                val c = async { serverClient.testConnection(url, secret) }
                val r = async { serverClient.fetchReservations(url, secret) }
                val w = async { serverClient.fetchWaterTracker(url, secret) }
                Triple(c.await(), r.await(), w.await())
            }
            renderConnection(conn)
            renderReservations(reservations)
            renderWater(water)
        }
    }

    private fun renderConnection(state: ConnectionState) {
        // LB-029/REQ-025: 서버에 닿지 않을 때만 '서버에 연결할 수 없음' 경고 카드를 보인다. 인증 실패(401)는 연결 줄 문구로 구분한다.
        val failed = state as? ConnectionState.Failed
        val unreachable = failed?.error == ConnectionError.UNREACHABLE
        cardServerWarning.visibility = if (unreachable) View.VISIBLE else View.GONE
        if (unreachable) textServerWarningMessage.text = failed?.message
        if (state is ConnectionState.Connected) {
            textConnectionLine.text = getString(R.string.home_conn_connected, state.serverUrl)
            textConnectionLine.setTextColor(ContextCompat.getColor(this, R.color.status_connected))
        } else {
            val msg = (state as? ConnectionState.Failed)?.message ?: getString(R.string.error_unknown)
            textConnectionLine.text = getString(R.string.home_conn_failed, msg)
            textConnectionLine.setTextColor(ContextCompat.getColor(this, R.color.status_error))
        }
    }

    private fun renderReservations(result: FetchReservationsResult) {
        when (result) {
            is FetchReservationsResult.Success -> {
                val now = ZonedDateTime.now(DateTimePickerLogic.KST)
                val next = HomeLogic.nextReservation(result.reservations, now)
                val c = HomeLogic.counts(result.reservations, now)
                textNextReservation.text = next?.formatKstDisplay() ?: getString(R.string.home_no_next_reservation)
                val counts = if (c.needsCheck > 0) {
                    getString(R.string.home_counts_needs_check, c.upcoming, c.past, c.needsCheck)
                } else {
                    getString(R.string.home_counts, c.upcoming, c.past)
                }
                textReservationCounts.text = counts
                textTileReservations.text = counts + "\n" + getString(R.string.home_counts_hint)
            }
            is FetchReservationsResult.Failure -> {
                val err = getString(R.string.home_section_error, result.message)
                textNextReservation.text = getString(R.string.home_loading_failed)
                textReservationCounts.text = err
                textTileReservations.text = err
            }
        }
    }

    private fun renderWater(result: WaterTrackerResult) {
        textTileWater.text = when (result) {
            is WaterTrackerResult.Success ->
                getString(R.string.home_water_summary, result.item.mopCleanCount, result.item.alertThresholdN)
            is WaterTrackerResult.Failure -> getString(R.string.home_section_error, result.message)
        }
    }

    private fun performRefill() {
        val url = ServerClient.getSavedServerUrl(this)
        val secret = ServerClient.getSavedSharedSecret(this)
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) { serverClient.refillWater(url, secret) }
            renderWater(result)
        }
    }
}
