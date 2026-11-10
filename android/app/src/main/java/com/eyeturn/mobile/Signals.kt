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
 * What calibration measured: the values while looking at the middle of the
 * screen, while glancing past each side of the phone, and with the head
 * tipped down / turned right.
 */
data class Calibration(
    val eyeX0: Float,
    val eyeRight: Float,
    val eyeLeft: Float,
    val yaw0: Float,
    val yawRight: Float,
    val pitch0: Float,
    val pitchDown: Float,
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

    /** Head turn from a neutral yaw: +1 = as far right as in calibration. */
    fun turn(yaw: Float, neutral: Float): Float {
        val r = yawRight - yaw0
        return if (abs(r) < 1e-3f) 0f else (yaw - neutral) / r
    }

    /** Head tip from a neutral pitch: +1 = as far down as in calibration. */
    fun tip(pitch: Float, neutral: Float): Float {
        val d = pitchDown - pitch0
        return if (abs(d) < 1e-3f) 0f else (pitch - neutral) / d
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
            abs(pitchDown - pitch0) < MIN_HEAD_DEG ->
                "Tip your head down a bit more next time."
            abs(yawRight - yaw0) < MIN_HEAD_DEG ->
                "Turn your head to the right a bit more next time."
            else -> null
        }
    }

    companion object {
        const val MIN_EYE = 0.06f
        const val MIN_HEAD_DEG = 3f
    }
}
