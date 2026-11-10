package com.eyeturn.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.util.Log
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService

/**
 * Keeps the front camera and face tracking running while you read in another
 * app. It's a foreground service (Android requires that for the camera in the
 * background), so there's always a notification with a Stop button.
 */
class TrackingService : LifecycleService() {
    private var tracker: FaceTracker? = null

    override fun onCreate() {
        super.onCreate()
        Engine.init(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            ServiceCompat.startForeground(
                this, NOTIFICATION_ID, notification(),
                if (Build.VERSION.SDK_INT >= 30) ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA else 0,
            )
        } catch (e: Exception) {
            Log.e("EyeTurn", "startForeground", e)
            Toast.makeText(this, "EyeTurn couldn't start the camera: open the app and press Start.", Toast.LENGTH_LONG).show()
            stopSelf()
            return START_NOT_STICKY
        }
        if (tracker == null) {
            tracker = FaceTracker(this, this, Engine::onSignals) { msg ->
                ContextCompat.getMainExecutor(this).execute {
                    Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
                }
            }.also { it.start() }
            Engine.setRunning(true)
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        tracker?.stop()
        tracker = null
        Engine.setRunning(false)
        super.onDestroy()
    }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL, "Eye tracking", NotificationManager.IMPORTANCE_LOW).apply {
                    description = "Shown while EyeTurn watches for glances"
                }
            )
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, TrackingService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_eye)
            .setContentTitle("EyeTurn is on")
            .setContentText("Glance past the edge of the screen to turn the page")
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(0, "Stop", stop)
            .build()
    }

    companion object {
        private const val CHANNEL = "tracking"
        private const val NOTIFICATION_ID = 1
        const val ACTION_STOP = "com.eyeturn.mobile.STOP"

        fun start(context: Context) {
            ContextCompat.startForegroundService(context, Intent(context, TrackingService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, TrackingService::class.java))
        }
    }
}
