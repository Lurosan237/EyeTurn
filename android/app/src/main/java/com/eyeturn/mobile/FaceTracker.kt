package com.eyeturn.mobile

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Matrix
import android.os.SystemClock
import android.util.Log
import android.util.Size
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import com.google.mediapipe.framework.image.BitmapImageBuilder
import com.google.mediapipe.tasks.core.BaseOptions
import com.google.mediapipe.tasks.core.Delegate
import com.google.mediapipe.tasks.vision.core.RunningMode
import com.google.mediapipe.tasks.vision.facelandmarker.FaceLandmarker
import com.google.mediapipe.tasks.vision.facelandmarker.FaceLandmarkerResult
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.asin
import kotlin.math.max
import kotlin.math.min

/**
 * Front camera -> MediaPipe Face Landmarker (the same model as EyeTurn desktop)
 * -> Signals, about 20 times a second.
 */
class FaceTracker(
    private val context: Context,
    private val owner: LifecycleOwner,
    private val onSignals: (Signals) -> Unit,
    private val onError: (String) -> Unit,
) {
    @Volatile private var landmarker: FaceLandmarker? = null
    private var executor: ExecutorService? = null
    private var provider: ProcessCameraProvider? = null
    private var lastFrameMs = 0L
    private var lastTs = 0L
    private val minFrameMs = 45L // ~20 fps is plenty and saves battery
    private var lastPreviewMs = 0L
    private var loggedFormat = false
    private var loggedFrameError = false
    private var frameSize = ""
    private val sentAt = LongArray(64)
    @Volatile private var inferMs = 0
    @Volatile private var frames = 0
    private var healthMs = SystemClock.uptimeMillis()
    private val recent = BooleanArray(40)
    private var recentIdx = 0
    private var recentCount = 0
    private var shownFace = false
    private var pendingFace = false
    private var pendingSince = 0L
    private var blinkL = 0f
    private var blinkR = 0f

    fun start() {
        val exec = Executors.newSingleThreadExecutor()
        executor = exec
        try {
            val base = BaseOptions.builder()
                .setModelAssetPath("face_landmarker.task")
                .setDelegate(Delegate.CPU)
                .build()
            val options = FaceLandmarker.FaceLandmarkerOptions.builder()
                .setBaseOptions(base)
                .setRunningMode(RunningMode.LIVE_STREAM)
                .setNumFaces(1)
                .setOutputFaceBlendshapes(true)
                .setOutputFacialTransformationMatrixes(true)
                .setMinFaceDetectionConfidence(0.4f)
                .setMinFacePresenceConfidence(0.4f)
                .setMinTrackingConfidence(0.4f)
                .setResultListener { result, _ -> handle(result, result.timestampMs()) }
                .setErrorListener { e -> Log.e(TAG, "face landmarker", e); Diag.log("face model error: $e") }
                .build()
            landmarker = FaceLandmarker.createFromOptions(context, options)
        } catch (e: Exception) {
            Log.e(TAG, "could not load the face model", e)
            Diag.setError("Could not load the face model: ${e.message}")
            onError("Could not load the face model: ${e.message}")
            return
        }

        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            try {
                val p = future.get()
                provider = p
                val analysis = ImageAnalysis.Builder()
                    .setResolutionSelector(
                        ResolutionSelector.Builder().setResolutionStrategy(
                            ResolutionStrategy(Size(640, 480), ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER)
                        ).build()
                    )
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
                    .build()
                analysis.setAnalyzer(exec) { analyze(it) }
                p.unbindAll()
                if (!p.hasCamera(CameraSelector.DEFAULT_FRONT_CAMERA)) {
                    Diag.setError("This phone has no front camera that apps can use")
                    onError("No front camera found")
                    return@addListener
                }
                p.bindToLifecycle(owner, CameraSelector.DEFAULT_FRONT_CAMERA, analysis)
                Diag.log("camera: front camera opened")
            } catch (e: Exception) {
                Log.e(TAG, "camera", e)
                Diag.setError("Could not open the front camera: ${e.message}")
                onError("Could not open the front camera: ${e.message}")
            }
        }, ContextCompat.getMainExecutor(context))
    }

    fun stop() {
        runCatching { provider?.unbindAll() }
        provider = null
        val lm = landmarker
        landmarker = null // analyze() skips frames from now on
        val exec = executor
        executor = null
        if (exec == null) {
            runCatching { lm?.close() }
            return
        }
        // Close the face model on the camera thread, after any frame still being sent to it:
        // closing it while a frame is in flight crashed the app when you pressed Stop.
        runCatching {
            exec.execute {
                runCatching { lm?.close() }
                Diag.log("face model closed")
            }
        }
        exec.shutdown()
    }

    private fun analyze(proxy: ImageProxy) {
        val now = SystemClock.uptimeMillis()
        val lm = landmarker
        if (lm == null || now - lastFrameMs < minFrameMs) {
            proxy.close()
            return
        }
        lastFrameMs = now
        try {
            val raw = proxy.toBitmap()
            val rotation = proxy.imageInfo.rotationDegrees
            proxy.close()
            if (!loggedFormat) {
                loggedFormat = true
                Diag.log("camera: first frame ${raw.width}x${raw.height}, rotation $rotation")
            }
            frameSize = "${raw.width}x${raw.height} rot $rotation"
            // Turn the frame upright and mirror it like a selfie before the face model sees it
            // (what Google's own MediaPipe sample does), so the face is always the right way up.
            val m = Matrix().apply {
                postRotate(rotation.toFloat())
                postScale(-1f, 1f)
            }
            val bitmap = Bitmap.createBitmap(raw, 0, 0, raw.width, raw.height, m, true)
            if (now - lastPreviewMs > 300) {
                lastPreviewMs = now
                val w = 160
                Diag.setFrame(Bitmap.createScaledBitmap(bitmap, w, (bitmap.height * w / bitmap.width).coerceAtLeast(1), true))
            }
            val ts = max(now, lastTs + 1)
            lastTs = ts
            sentAt[(ts % sentAt.size).toInt()] = now
            frames++
            lm.detectAsync(BitmapImageBuilder(bitmap).build(), ts)
        } catch (e: Exception) {
            proxy.close()
            Log.w(TAG, "frame", e)
            if (!loggedFrameError) { loggedFrameError = true; Diag.log("frame error: $e") }
        }
    }

    private fun handle(result: FaceLandmarkerResult, tsMs: Long) {
        val nowMs = SystemClock.uptimeMillis()
        val t = nowMs / 1000.0
        inferMs = (nowMs - sentAt[(tsMs % sentAt.size).toInt()]).toInt()
        val shapes = result.faceBlendshapes().orElse(null)
        val face = !shapes.isNullOrEmpty()
        noteFace(face, nowMs)
        if (!face) {
            onSignals(Signals(t, false))
            return
        }
        val b = HashMap<String, Float>(64)
        for (c in shapes!![0]) b[c.categoryName()] = c.score()
        fun v(k: String) = b[k] ?: 0f
        // Positive when both eyes look toward the face's left (sign is learned by calibration anyway).
        val eyeX = ((v("eyeLookOutLeft") - v("eyeLookInLeft")) + (v("eyeLookInRight") - v("eyeLookOutRight"))) / 2f
        val eyeY = ((v("eyeLookDownLeft") + v("eyeLookDownRight")) - (v("eyeLookUpLeft") + v("eyeLookUpRight"))) / 2f
        blinkL = v("eyeBlinkLeft")
        blinkR = v("eyeBlinkRight")
        // Looking down at a phone half-closes the eyelids, so one eye's score alone runs high.
        // Only both eyes closed counts as a blink.
        val blink = min(blinkL, blinkR)

        var yaw = 0f
        var pitch = 0f
        val mats = result.facialTransformationMatrixes().orElse(null)
        if (!mats.isNullOrEmpty() && mats[0].size >= 16) {
            val m = mats[0]
            // Elements 8 and 9 are the left-right and up-down tilt of the face's forward
            // axis whichever way the matrix is laid out (only the sign can differ).
            yaw = Math.toDegrees(asin(m[8].coerceIn(-1f, 1f).toDouble())).toFloat()
            pitch = Math.toDegrees(asin(m[9].coerceIn(-1f, 1f).toDouble())).toFloat()
        }
        onSignals(Signals(t, true, eyeX, eyeY, yaw, pitch, blink))
    }

    /** Keeps the camera health numbers and logs when the face comes and goes. */
    private fun noteFace(face: Boolean, nowMs: Long) {
        recent[recentIdx] = face
        recentIdx = (recentIdx + 1) % recent.size
        if (recentCount < recent.size) recentCount++
        if (face != shownFace) {
            if (face != pendingFace) { pendingFace = face; pendingSince = nowMs }
            if (nowMs - pendingSince > 1000) {
                shownFace = face
                Diag.log(if (face) "face found" else "face lost")
            }
        } else pendingFace = face
        if (nowMs - healthMs >= 1000) {
            val fps = frames * 1000f / (nowMs - healthMs).coerceAtLeast(1)
            frames = 0
            healthMs = nowMs
            val rate = (0 until recentCount).count { recent[it] } / recentCount.coerceAtLeast(1).toFloat()
            Diag.setHealth(CameraHealth(fps, rate, inferMs, frameSize, blinkL, blinkR, Diag.health.value.error))
        }
    }

    companion object { private const val TAG = "EyeTurn" }
}
