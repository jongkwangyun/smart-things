package com.smartthings.cleaner

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.smartthings.cleaner.model.DeleteOffPeriodResult
import com.smartthings.cleaner.model.FetchOffPeriodsResult
import com.smartthings.cleaner.model.OffPeriodItem
import com.smartthings.cleaner.network.ServerClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 쉬는 기간 목록 화면 (LB-021, LB-022, REQ-012, REQ-013).
 *
 * SPEC:
 * - done_when: 진행 중인 쉬는 기간을 삭제하면 목록에서 사라지고, 직후 반복 예약 청소가 시작되어도 정지되지 않는다 (LB-022)
 * - must: 삭제 즉시 제한이 해제되어야 한다.
 * - REQ-012: 등록된 쉬는 기간 목록 조회
 * - REQ-013: 쉬는 기간 삭제
 * - REQ-010: 두 사용자 모두 상대방이 만든 예약과 쉬는 기간을 조회
 * - D-09: 두 사용자 동일 권한
 * - REQ-028 / REQ-029: S21+ 및 Fold8(접힘/펼침 반응형) 지원
 */
class OffPeriodListActivity : AppCompatActivity() {

    data class StatusStyle(
        val label: String,
        val textColorRes: Int,
        val bgColorRes: Int,
        val strokeColorRes: Int
    )

    companion object {
        fun getStatusStyle(status: String): StatusStyle {
            return when (status) {
                OffPeriodItem.STATUS_ACTIVE -> StatusStyle(
                    label = "진행 중",
                    textColorRes = R.color.status_off_active,
                    bgColorRes = R.color.status_off_active_bg,
                    strokeColorRes = R.color.badge_border_off_active
                )
                OffPeriodItem.STATUS_UPCOMING -> StatusStyle(
                    label = "예정",
                    textColorRes = R.color.status_off_upcoming,
                    bgColorRes = R.color.status_off_upcoming_bg,
                    strokeColorRes = R.color.badge_border_off_upcoming
                )
                OffPeriodItem.STATUS_ENDED -> StatusStyle(
                    label = "종료",
                    textColorRes = R.color.status_off_ended,
                    bgColorRes = R.color.status_off_ended_bg,
                    strokeColorRes = R.color.badge_border_off_ended
                )
                else -> StatusStyle(
                    label = status,
                    textColorRes = R.color.text_secondary,
                    bgColorRes = R.color.status_disconnected_bg,
                    strokeColorRes = R.color.border_color
                )
            }
        }
    }

    private val serverClient = ServerClient()

    private lateinit var btnRefresh: MaterialButton
    private lateinit var btnAddOffPeriod: MaterialButton
    private lateinit var progressBar: ProgressBar

    private lateinit var cardError: MaterialCardView
    private lateinit var textErrorMessage: TextView

    private lateinit var textListHeader: TextView
    private lateinit var textEmptyList: TextView
    private lateinit var layoutOffPeriodList: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_off_period_list)

        initViews()
        loadOffPeriods()
    }

    override fun onResume() {
        super.onResume()
        loadOffPeriods()
    }

    private fun getCredentials(): Pair<String, String> = Pair(
        intent.getStringExtra("EXTRA_SERVER_URL") ?: ServerClient.getSavedServerUrl(this),
        intent.getStringExtra("EXTRA_SHARED_SECRET") ?: ServerClient.getSavedSharedSecret(this)
    )

    private fun initViews() {
        btnRefresh = findViewById(R.id.btnRefresh)
        btnAddOffPeriod = findViewById(R.id.btnAddOffPeriod)
        progressBar = findViewById(R.id.progressBar)

        cardError = findViewById(R.id.cardError)
        textErrorMessage = findViewById(R.id.textErrorMessage)

        textListHeader = findViewById(R.id.textListHeader)
        textEmptyList = findViewById(R.id.textEmptyList)
        layoutOffPeriodList = findViewById(R.id.layoutOffPeriodList)

        btnRefresh.setOnClickListener {
            loadOffPeriods()
        }

        btnAddOffPeriod.setOnClickListener {
            val (serverUrl, sharedSecret) = getCredentials()
            val createIntent = Intent(this, CreateOffPeriodActivity::class.java).apply {
                putExtra("EXTRA_SERVER_URL", serverUrl)
                putExtra("EXTRA_SHARED_SECRET", sharedSecret)
            }
            startActivity(createIntent)
        }
    }

    fun loadOffPeriods() {
        val (serverUrl, sharedSecret) = getCredentials()
        if (serverUrl.isBlank() || sharedSecret.isBlank()) {
            showError("서버 주소와 비밀키가 설정되지 않았습니다. 메인 화면에서 먼저 서버에 연결해 주세요.")
            return
        }

        progressBar.visibility = View.VISIBLE
        btnRefresh.isEnabled = false
        cardError.visibility = View.GONE

        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.fetchOffPeriods(serverUrl, sharedSecret)
            withContext(Dispatchers.Main) {
                progressBar.visibility = View.GONE
                btnRefresh.isEnabled = true
                handleFetchResult(result)
            }
        }
    }

    fun handleFetchResult(result: FetchOffPeriodsResult) {
        when (result) {
            is FetchOffPeriodsResult.Success -> {
                cardError.visibility = View.GONE
                renderOffPeriods(result.offPeriods)
            }
            is FetchOffPeriodsResult.Failure -> {
                val errorMsg = when (result.statusCode) {
                    401 -> "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)"
                    0 -> "서버에 연결할 수 없습니다. 서버 주소와 네트워크를 확인하세요. (REQ-025)"
                    else -> result.message
                }
                showError(errorMsg)
            }
        }
    }

    private fun showError(message: String) {
        cardError.visibility = View.VISIBLE
        textErrorMessage.text = message
    }

    fun renderOffPeriods(items: List<OffPeriodItem>) {
        textListHeader.text = getString(R.string.section_off_periods_header) + " (${items.size}건)"
        layoutOffPeriodList.removeAllViews()

        if (items.isEmpty()) {
            textEmptyList.visibility = View.VISIBLE
            layoutOffPeriodList.visibility = View.GONE
        } else {
            textEmptyList.visibility = View.GONE
            layoutOffPeriodList.visibility = View.VISIBLE

            val inflater = LayoutInflater.from(this)
            for (item in items) {
                val itemView = inflater.inflate(R.layout.item_off_period, layoutOffPeriodList, false)
                bindItemView(itemView, item)
                layoutOffPeriodList.addView(itemView)
            }
        }
    }

    fun bindItemView(view: View, item: OffPeriodItem) {
        val textOffPeriodId = view.findViewById<TextView>(R.id.textOffPeriodId)
        val cardStatusBadge = view.findViewById<MaterialCardView>(R.id.cardStatusBadge)
        val textStatusBadge = view.findViewById<TextView>(R.id.textStatusBadge)
        val textStartAt = view.findViewById<TextView>(R.id.textStartAt)
        val textEndAt = view.findViewById<TextView>(R.id.textEndAt)
        val textCreatedAt = view.findViewById<TextView>(R.id.textCreatedAt)
        val btnDeleteOffPeriod = view.findViewById<MaterialButton?>(R.id.btnDeleteOffPeriod)

        textOffPeriodId.text = "ID: ${item.id}"
        textStartAt.text = "시작: ${item.formatStartKst()}"
        textEndAt.text = "종료: ${item.formatEndKst()}"
        textCreatedAt.text = if (item.createdAt.isNotEmpty()) "등록: ${item.createdAt}" else ""

        val status = item.getStatus()
        val style = getStatusStyle(status)
        textStatusBadge.text = style.label
        textStatusBadge.setTextColor(ContextCompat.getColor(this, style.textColorRes))
        cardStatusBadge.setCardBackgroundColor(ContextCompat.getColor(this, style.bgColorRes))
        cardStatusBadge.strokeColor = ContextCompat.getColor(this, style.strokeColorRes)

        btnDeleteOffPeriod?.setOnClickListener {
            showDeleteConfirmDialog(item)
        }
    }

    fun showDeleteConfirmDialog(item: OffPeriodItem) {
        AlertDialog.Builder(this)
            .setTitle("쉬는 기간 삭제")
            .setMessage("이 쉬는 기간을 삭제하시겠습니까?")
            .setPositiveButton("삭제") { _, _ -> performDeleteOffPeriod(item.id) }
            .setNegativeButton("취소", null)
            .show()
    }

    fun performDeleteOffPeriod(offPeriodId: String) {
        val (serverUrl, sharedSecret) = getCredentials()
        if (serverUrl.isBlank() || sharedSecret.isBlank()) return showError("서버 주소와 비밀키가 설정되지 않았습니다.")
        progressBar.visibility = View.VISIBLE
        lifecycleScope.launch(Dispatchers.IO) {
            val result = serverClient.deleteOffPeriod(serverUrl, sharedSecret, offPeriodId)
            withContext(Dispatchers.Main) {
                progressBar.visibility = View.GONE
                when (result) {
                    is DeleteOffPeriodResult.Success -> {
                        Toast.makeText(this@OffPeriodListActivity, "쉬는 기간이 삭제되었습니다.", Toast.LENGTH_SHORT).show()
                        loadOffPeriods()
                    }
                    is DeleteOffPeriodResult.Failure -> {
                        val errorMsg = when (result.statusCode) {
                            401 -> "인증 실패: 공유 비밀키가 올바르지 않습니다. (REQ-024, D-09)"
                            404 -> "삭제할 쉬는 기간을 찾을 수 없습니다."
                            0 -> "서버에 연결할 수 없습니다. 서버 주소와 네트워크를 확인하세요. (REQ-025)"
                            else -> result.message
                        }
                        showError(errorMsg)
                    }
                }
            }
        }
    }
}
