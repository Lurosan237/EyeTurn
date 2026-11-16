package com.eyeturn.mobile

import android.content.Context
import android.graphics.Bitmap
import android.os.Build
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/** Live camera health, shown on the "Camera check" card. */
data class CameraHealth(
    val fps: Float = 0f,          // frames analysed per second
    val faceRate: Float = 0f,     // share of recent frames with a face, 0..1
    val inferMs: Int = 0,         // time the face model takes per frame
    val frameSize: String = "",   // e.g. "480x640 rot 270"
    val blinkL: Float = 0f,
    val blinkR: Float = 0f,
    val error: String? = null,
)

/**
 * A small log kept on the phone (last ~300 lines, also saved to a file) and the
 * latest camera frame, so problems can be seen on screen and sent with "Share log".
 */
object Diag {
    private val lines = ArrayDeque<String>()
    private var file: File? = null
    private val fmt = SimpleDateFormat("MM-dd HH:mm:ss.SSS", Locale.US)

    private val _health = MutableStateFlow(CameraHealth())
    val health: StateFlow<CameraHealth> = _health

    /** What the face model sees (small, upright, mirrored like a selfie). */
    private val _frame = MutableStateFlow<Bitmap?>(null)
    val frame: StateFlow<Bitmap?> = _frame

    fun init(context: Context) {
        if (file != null) return
        val f = File(context.filesDir, "eyeturn-log.txt")
        file = f
        runCatching {
            if (f.exists()) {
                // Keep the end of the previous runs.
                f.readLines().takeLast(150).forEach { lines.addLast(it) }
            }
        }
        log("--- app start: EyeTurn ${BuildConfig.VERSION_NAME} on ${Build.MANUFACTURER} ${Build.MODEL}, Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})")
        // Write any crash into the log before Android ends the app, so Share log shows it.
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, e ->
            runCatching { log("CRASH in ${thread.name}: ${android.util.Log.getStackTraceString(e).take(3000)}") }
            previous?.uncaughtException(thread, e)
        }
    }

    @Synchronized
    fun log(msg: String) {
        val line = "${fmt.format(Date())} $msg"
        android.util.Log.i("EyeTurn", msg)
        lines.addLast(line)
        while (lines.size > 300) lines.removeFirst()
        runCatching {
            val f = file ?: return@runCatching
            if (f.length() > 200_000) f.writeText(lines.joinToString("\n", postfix = "\n")) else f.appendText(line + "\n")
        }
    }

    @Synchronized
    fun text(): String = lines.joinToString("\n")

    fun setHealth(h: CameraHealth) { _health.value = h }
    fun setError(e: String) { _health.value = _health.value.copy(error = e); log("ERROR $e") }
    fun setFrame(b: Bitmap) { _frame.value = b }
}
