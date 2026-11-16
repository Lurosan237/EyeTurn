package com.eyeturn.mobile

import kotlin.math.abs

/**
 * One camera frame, boiled down to what EyeTurn needs.
 *
 * eyeX: where the eyes point left-right, from MediaPipe's eye blendshapes
 * (eyeLookIn/eyeLookOut). Its sign depends on the camera, so calibration
 * learns which way is "right".
 * yaw / pitch: head direction in degrees, from the face transformation matrix
 * (sign also learned by calibration).
 */
data class Signals(
    val t: Double,          // seconds, monotonic
    val face: Boolean,      // false = no face in view
    val eyeX: Float = 0f,
    val eyeY: Float = 0f,
    val yaw: Float = 0f,
    val pitch: Float = 0f,
    val blink: Float = 0f,  // 0..1, the more closed eye
)

/**
 * What calibration measured: the eyes while looking at the middle of the
 * screen and while glancing past each side of the phone. Only Eyes mode needs
 * it; Head mode works without calibration, like on the PC.
 */
data class Calibration(
    val eyeX0: Float,
    val eyeRight: Float,
    val eyeLeft: Float,
) {
    /** Glance: 0 = middle of the screen, +1 = as far right as the calibration glance, -1 = as far left. */
    fun glance(eyeX: Float): Float {
        val d = eyeX - eyeX0
        val r = eyeRight - eyeX0
        val l = eyeLeft - eyeX0
        return when {
            r != 0f && d * r > 0f -> d / r
            l != 0f && d * l > 0f -> -d / l
            else -> 0f
        }
    }

    /** Checks the calibration is usable. Returns a problem to show, or null when it's fine. */
    fun problem(): String? {
        val r = eyeRight - eyeX0
        val l = eyeLeft - eyeX0
        return when {
            abs(r) < MIN_EYE || abs(l) < MIN_EYE ->
                "Your glances were too small to tell apart. Try again and look clearly past the edge of the phone."
            r * l > 0f ->
                "The left and right glances looked the same. Try again, moving only your eyes."
            else -> null
        }
    }

    companion object {
        const val MIN_EYE = 0.06f
    }
}
