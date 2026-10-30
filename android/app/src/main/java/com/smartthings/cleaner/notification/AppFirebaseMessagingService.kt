package com.smartthings.cleaner.notification

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.smartthings.cleaner.MainActivity
import com.smartthings.cleaner.network.ServerClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.util.UUID

/**
 * FCM 푸시 알림 수신 및 로컬 알림 표시 서비스 (D-12, REQ-022).
 *
 * SPEC:
 * - done_when: 서버에서 시험 알림을 한 번 보내면 갤럭시 S21+와 갤럭시 폴드8 두 대 모두의 알림창에 같은 알림이 뜬다.
 * - D-12: 알림 채널은 앱 푸시(FCM), 두 사용자 모두 수신.
 */
class AppFirebaseMessagingService : FirebaseMessagingService() {

    companion object {
        const val CHANNEL_ID = "cleaner_notifications"
        const val CHANNEL_NAME = "로봇청소기 알림"
        const val CHANNEL_DESC = "로봇청소기 상태 및 시험 알림 채널"

        private const val PREFS_PUSH = "cleaner_push_prefs"
        private const val KEY_PUSH_TOKEN = "key_fcm_token"
        private const val KEY_DEVICE_ID = "key_fcm_device_id"

        fun getSavedPushToken(context: Context): String =
            context.getSharedPreferences(PREFS_PUSH, Context.MODE_PRIVATE)
                .getString(KEY_PUSH_TOKEN, "") ?: ""

        fun savePushToken(context: Context, token: String) {
            context.getSharedPreferences(PREFS_PUSH, Context.MODE_PRIVATE).edit()
                .putString(KEY_PUSH_TOKEN, token.trim())
                .apply()
        }

        fun getOrCreateDeviceId(context: Context): String {
            val prefs = context.getSharedPreferences(PREFS_PUSH, Context.MODE_PRIVATE)
            var devId = prefs.getString(KEY_DEVICE_ID, null)
            if (devId.isNullOrEmpty()) {
                devId = "device-${UUID.randomUUID().toString().substring(0, 8)}"
                prefs.edit().putString(KEY_DEVICE_ID, devId).apply()
            }
            return devId
        }

        /** 알림 채널 생성 (Android 8.0+ O) */
        fun createNotificationChannel(context: Context) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
                    ?: return
                val existing = manager.getNotificationChannel(CHANNEL_ID)
                if (existing == null) {
                    val channel = NotificationChannel(
                        CHANNEL_ID,
                        CHANNEL_NAME,
                        NotificationManager.IMPORTANCE_HIGH
                    ).apply {
                        description = CHANNEL_DESC
                        enableLights(true)
                        enableVibration(true)
                    }
                    manager.createNotificationChannel(channel)
                }
            }
        }

        /** 시스템 알림창에 푸시 알림 표시 (done_when 기준) */
        fun showNotification(
            context: Context,
            title: String,
            body: String,
            notificationId: Int = 1001
        ) {
            createNotificationChannel(context)

            val intent = Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
            }
            val pendingIntent = PendingIntent.getActivity(
                context,
                0,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            val builder = NotificationCompat.Builder(context, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(NotificationCompat.BigTextStyle().bigText(body))
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setAutoCancel(true)
                .setContentIntent(pendingIntent)

            val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
            manager?.notify(notificationId, builder.build())
        }
    }

    override fun onNewToken(token: String) {
        super.onNewToken(token)
        savePushToken(this, token)

        val deviceId = getOrCreateDeviceId(this)
        val serverUrl = ServerClient.getSavedServerUrl(this)
        val sharedSecret = ServerClient.getSavedSharedSecret(this)

        if (serverUrl.isNotEmpty() && sharedSecret.isNotEmpty()) {
            CoroutineScope(Dispatchers.IO).launch {
                ServerClient().registerNotificationTarget(serverUrl, sharedSecret, deviceId, token)
            }
        }
    }

    override fun onMessageReceived(remoteMessage: RemoteMessage) {
        super.onMessageReceived(remoteMessage)

        val title = remoteMessage.notification?.title
            ?: remoteMessage.data["title"]
            ?: "로봇청소기 알림"
        val body = remoteMessage.notification?.body
            ?: remoteMessage.data["body"]
            ?: ""

        showNotification(this, title, body)
    }
}
