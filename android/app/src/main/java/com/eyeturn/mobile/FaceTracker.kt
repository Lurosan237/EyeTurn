package com.eyeturn.mobile

import android.content.Context
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
import com.google.mediapipe.tasks.vision.core.ImageProcessingOptions
import com.google.mediapipe.tasks.vision.core.RunningMode
import com.google.mediapipe.tasks.vision.facelandmarker.FaceLandmarker
import com.google.mediapipe.tasks.vision.facelandmarker.FaceLandmarkerResult
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.asin
import kotlin.math.max

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
    private var landmarker: FaceLandmarker? = null
    private var executor: ExecutorService? = null
    private var provider: ProcessCameraProvider? = null
    private var lastFrameMs = 0L
    private var lastTs = 0L
    private val minFrameMs = 45L // ~20 fps is plenty and saves battery

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
                .setMinFaceDetectionConfidence(0.5f)
                .setMinFacePresenceConfidence(0.5f)
                .setMinTrackingConfidence(0.5f)
                .setResultListener { result, _ -> handle(result) }
                .setErrorListener { e -> Log.e(TAG, "face landmarker", e) }
                .build()
            landmarker = FaceLandmarker.createFromOptions(context, options)
        } catch (e: Exception) {
            Log.e(TAG, "could not load the face model", e)
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
                p.bindToLifecycle(owner, CameraSelector.DEFAULT_FRONT_CAMERA, analysis)
            } catch (e: Exception) {
                Log.e(TAG, "camera", e)
                onError("Could not open the front camera: ${e.message}")
            }
        }, ContextCompat.getMainExecutor(context))
    }

    fun stop() {
        runCatching { provider?.unbindAll() }
        provider = null
        executor?.shutdown()
        executor = null
        runCatching { landmarker?.close() }
        landmarker = null
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
            val bitmap = proxy.toBitmap()
            val rotation = proxy.imageInfo.rotationDegrees
            proxy.close()
            val ts = max(now, lastTs + 1)
            lastTs = ts
            lm.detectAsync(
                BitmapImageBuilder(bitmap).build(),
                ImageProcessingOptions.builder().setRotationDegrees(rotation).build(),
                ts,
            )
        } catch (e: Exception) {
            proxy.close()
            Log.w(TAG, "frame", e)
        }
    }

    private fun handle(result: FaceLandmarkerResult) {
        val t = SystemClock.uptimeMillis() / 1000.0
        val shapes = result.faceBlendshapes().orElse(null)
        if (shapes.isNullOrEmpty()) {
            onSignals(Signals(t, false))
            return
        }
        val b = HashMap<String, Float>(64)
        for (c in shapes[0]) b[c.categoryName()] = c.score()
        fun v(k: String) = b[k] ?: 0f
        // Positive when both eyes look toward the face's left (sign is learned by calibration anyway).
        val eyeX = ((v("eyeLookOutLeft") - v("eyeLookInLeft")) + (v("eyeLookInRight") - v("eyeLookOutRight"))) / 2f
        val eyeY = ((v("eyeLookDownLeft") + v("eyeLookDownRight")) - (v("eyeLookUpLeft") + v("eyeLookUpRight"))) / 2f
        val blink = max(v("eyeBlinkLeft"), v("eyeBlinkRight"))

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

    companion object { private const val TAG = "EyeTurn" }
}
