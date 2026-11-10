package com.eyeturn.mobile

import android.content.Context
import android.content.SharedPreferences

enum class TurnStyle { SCROLL, SWIPE }

/** Everything the user sets, plus the calibration, kept in SharedPreferences. */
class Settings(context: Context) {
    val prefs: SharedPreferences = context.applicationContext.getSharedPreferences("eyeturn", Context.MODE_PRIVATE)

    var mode: Mode
        get() = runCatching { Mode.valueOf(prefs.getString("mode", null) ?: "EYES") }.getOrDefault(Mode.EYES)
        set(v) = prefs.edit().putString("mode", v.name).apply()

    var turnStyle: TurnStyle
        get() = runCatching { TurnStyle.valueOf(prefs.getString("turn_style", null) ?: "SCROLL") }.getOrDefault(TurnStyle.SCROLL)
        set(v) = prefs.edit().putString("turn_style", v.name).apply()

    var glanceSensitivity: Float
        get() = prefs.getFloat("glance_sensitivity", 0.5f)
        set(v) = prefs.edit().putFloat("glance_sensitivity", v).apply()

    var speed: Float
        get() = prefs.getFloat("speed", 1f)
        set(v) = prefs.edit().putFloat("speed", v).apply()

    /** Share of the screen kept on screen after a page turn, so you don't lose your line. */
    var overlap: Float
        get() = prefs.getFloat("overlap", 0.1f)
        set(v) = prefs.edit().putFloat("overlap", v).apply()

    var holdScroll: Boolean
        get() = prefs.getBoolean("hold_scroll", true)
        set(v) = prefs.edit().putBoolean("hold_scroll", v).apply()

    var showDot: Boolean
        get() = prefs.getBoolean("show_dot", true)
        set(v) = prefs.edit().putBoolean("show_dot", v).apply()

    var calibration: Calibration?
        get() {
            if (!prefs.contains("cal_eyeX0")) return null
            fun f(k: String) = prefs.getFloat("cal_$k", 0f)
            return Calibration(f("eyeX0"), f("eyeRight"), f("eyeLeft"), f("yaw0"), f("yawRight"), f("pitch0"), f("pitchDown"))
        }
        set(c) {
            val e = prefs.edit()
            if (c == null) {
                listOf("eyeX0", "eyeRight", "eyeLeft", "yaw0", "yawRight", "pitch0", "pitchDown").forEach { e.remove("cal_$it") }
            } else {
                e.putFloat("cal_eyeX0", c.eyeX0).putFloat("cal_eyeRight", c.eyeRight).putFloat("cal_eyeLeft", c.eyeLeft)
                    .putFloat("cal_yaw0", c.yaw0).putFloat("cal_yawRight", c.yawRight)
                    .putFloat("cal_pitch0", c.pitch0).putFloat("cal_pitchDown", c.pitchDown)
            }
            e.apply()
        }

    fun tuning() = Tuning(mode = mode, glanceSensitivity = glanceSensitivity, holdScroll = holdScroll, speed = speed)
}
