package com.smartthings.cleaner

import android.content.Context
import androidx.appcompat.app.AlertDialog

/**
 * 물 채움 실행 전 확인 대화상자 (LB-053, LB-060, REQ-019, REQ-021, D-14).
 *
 * 물 채움 바로가기를 두는 모든 화면(물 관리 화면, 홈 타일 LB-047)이 공용으로 사용한다.
 * 일반 물 채움(횟수만 0 초기화)과 물 부족으로 채움(N = max(1, K-1) 자동 보정 후 0 초기화)을 선택할 수 있다.
 * 취소/바깥 터치/뒤로가기는 아무것도 호출하지 않는다 (LB-053 준수).
 */
object RefillConfirmDialog {

    /** 물 부족 시 자동 보정될 알림 기준값 N = max(1, K - 1) 계산 (REQ-021, docs/device-capabilities.md 섹션 10). */
    fun calculateCalibratedThreshold(mopCleanCount: Int): Int = maxOf(1, mopCleanCount - 1)

    fun show(
        context: Context,
        mopCleanCount: Int,
        onConfirmed: (calibrateThreshold: Boolean) -> Unit
    ): AlertDialog {
        val builder = AlertDialog.Builder(context)
            .setTitle(R.string.dialog_title_confirm_refill)
            .setNegativeButton(R.string.btn_cancel, null)

        if (mopCleanCount >= 1) {
            val calibrated = calculateCalibratedThreshold(mopCleanCount)
            val msg = context.getString(R.string.dialog_msg_confirm_refill_calibration_guide, mopCleanCount, calibrated)
            val btnCalibrated = context.getString(R.string.btn_refill_with_calibration, calibrated)
            builder.setMessage(msg)
                .setPositiveButton(btnCalibrated) { _, _ -> onConfirmed(true) }
                .setNeutralButton(R.string.btn_refill_normal) { _, _ -> onConfirmed(false) }
        } else {
            builder.setMessage(R.string.dialog_msg_confirm_refill)
                .setPositiveButton(R.string.btn_refill_with_calibration_simple) { _, _ -> onConfirmed(true) }
                .setNeutralButton(R.string.btn_refill_normal) { _, _ -> onConfirmed(false) }
        }

        return builder.show()
    }

    /** 기존 단일 콜백 호출 호환용 (LB-053 호환) */
    fun show(context: Context, onConfirmed: () -> Unit): AlertDialog =
        show(context, 0) { _ -> onConfirmed() }
}
