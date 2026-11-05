package com.smartthings.cleaner

import android.content.Context
import androidx.appcompat.app.AlertDialog

/**
 * 물 채움 실행 전 확인 대화상자 (LB-053, REQ-019).
 *
 * 물 채움 바로가기를 두는 모든 화면(물 관리 화면, 홈 타일 LB-047)이 공용으로 사용한다.
 * [onConfirmed] 는 확인 버튼을 눌렀을 때에만 실행된다. 취소/바깥 터치/뒤로가기는 아무것도 호출하지 않는다.
 */
object RefillConfirmDialog {

    fun show(context: Context, onConfirmed: () -> Unit): AlertDialog =
        AlertDialog.Builder(context)
            .setTitle(R.string.dialog_title_confirm_refill)
            .setMessage(R.string.dialog_msg_confirm_refill)
            .setPositiveButton(R.string.btn_refill_confirm) { _, _ -> onConfirmed() }
            .setNegativeButton(R.string.btn_cancel, null)
            .show()
}
