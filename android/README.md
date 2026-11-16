# EyeTurn for Android

The phone version of EyeTurn. It runs in the background while you read in any
app (WPS Office, Chrome, Kindle, Google Play Books...) and turns pages or
scrolls when you:

- **Eyes mode:** glance past the right edge of the phone for the next page,
  past the left edge for the previous page. Keep looking past the edge for a
  second and it keeps scrolling, faster over 3 seconds, until you look back.
- **Head mode:** tip your head down to scroll down, up to scroll up (faster
  the further you tip). Turn your head right and hold it for the next page,
  left for the previous one.

It uses the same MediaPipe face model as EyeTurn on Windows, on the phone
itself. Camera images never leave the phone (the app has no internet
permission).

## Install

1. Copy `EyeTurn.apk` to the phone (USB cable, Google Drive, or email it to yourself).
2. Open it on the phone and allow installing from that source when Android asks.
   If Play Protect warns about an unknown app, choose "Install anyway".
3. Open EyeTurn and follow the setup card:
   1. Allow the camera.
   2. Allow the notification (it shows while EyeTurn is on, with a Stop button).
   3. Turn EyeTurn on in **Settings > Accessibility > EyeTurn**. That is what lets
      it scroll other apps. On Android 13 and newer, an app installed from a file
      is blocked at first: open **App info** (button in EyeTurn), tap **⋮** at the
      top right, choose **Allow restricted settings**, then turn it on again.
   4. Calibrate (about 20 seconds): look at the middle, glance right, middle,
      glance left, tip your head down, turn your head right. A beep ends each step.
4. Press **Start**, then open your book or PDF.

A small dot at the top right shows the state: green = sees you, blue = a
gesture is starting, grey = no face, orange = paused. Tap it to pause or
resume, hold it to open EyeTurn.

## Settings

- **Page turn: Scroll** drags the page up by one screen minus the overlap (PDFs,
  web pages, WPS in continuous mode). **Swipe** swipes sideways (book apps and
  WPS in page-by-page mode).
- **Glance sensitivity:** higher = a smaller glance is enough. Lower it if pages
  turn while you read; raise it if glances are missed.
- **Scroll speed**, **lines kept after a page turn**, **keep looking to keep
  scrolling**, **show the status dot**.

## Build

Needs JDK 17+ and the Android SDK (platform 35). From this folder:

```
gradlew assembleRelease
```

The APK is `app/build/outputs/apk/release/app-release.apk`. The face model is
downloaded on the first build. To sign with your own key, add
`keystore.properties` (storeFile, storePassword, keyAlias, keyPassword) next
to this README; without it the debug key is used.

`gradlew testReleaseUnitTest` runs simulated-face tests of the glance and head rules.

## How it maps to the desktop code

| Desktop (`eyeturn/`) | Android (`app/src/main/java/com/eyeturn/mobile/`) |
| --- | --- |
| `tracker.py` (MediaPipe face) | `FaceTracker.kt` (front camera via CameraX) |
| `actions.py` glance turn + hold scroll | `Controller.kt` `GlanceControl` |
| `head.py` | `Controller.kt` `HeadControl` |
| `winput.py` (wheel / keys to Windows) | `GestureService.kt` (touch gestures via Accessibility) |
| `calibration.py` | `CalibrationScreen.kt` (6 quick steps, no dot grid) |

On a phone the camera is close and the screen is small, so EyeTurn uses
deliberate glances and head moves rather than tracking exactly where on the
page you look.
