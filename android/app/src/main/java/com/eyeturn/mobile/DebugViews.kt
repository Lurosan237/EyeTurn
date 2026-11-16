package com.eyeturn.mobile

import android.content.Intent
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.foundation.background

/** The picture the face model gets (upright, mirrored like a selfie). */
@Composable
fun CameraThumb(small: Boolean = false) {
    val frame by Diag.frame.collectAsState()
    val w = if (small) 72.dp else 120.dp
    val b = frame
    if (b == null) {
        Box(Modifier.size(w, w * 4 / 3).background(MaterialTheme.colorScheme.surfaceVariant), contentAlignment = Alignment.Center) {
            Text("No picture yet", fontSize = 11.sp)
        }
    } else {
        Image(b.asImageBitmap(), contentDescription = "What the camera sees", modifier = Modifier.width(w))
    }
}

/** Camera picture plus the numbers that tell what's wrong when the face isn't found. */
@Composable
fun CameraCheck() {
    val h by Diag.health.collectAsState()
    val signals by Engine.signals.collectAsState()
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        CameraThumb()
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(if (signals.face) "Face: found" else "Face: not found", fontSize = 14.sp)
            Text("Face in ${(h.faceRate * 100).toInt()}% of pictures", fontSize = 13.sp)
            Text("%.0f pictures a second".format(h.fps), fontSize = 13.sp)
            Text("Face model: ${h.inferMs} ms", fontSize = 13.sp)
            Text("Eyelids: left %.2f, right %.2f".format(h.blinkL, h.blinkR), fontSize = 13.sp)
            if (h.frameSize.isNotEmpty()) Text("Camera: ${h.frameSize}", fontSize = 12.sp)
            h.error?.let { Text("Problem: $it", fontSize = 13.sp, color = MaterialTheme.colorScheme.error) }
        }
    }
}

/** Sends the log (as text) to any app: email, WhatsApp, Drive... */
@Composable
fun ShareLogButton() {
    val ctx = LocalContext.current
    OutlinedButton(onClick = {
        val send = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_SUBJECT, "EyeTurn log")
            putExtra(Intent.EXTRA_TEXT, Diag.text())
        }
        ctx.startActivity(Intent.createChooser(send, "Send the EyeTurn log"))
    }) { Text("Share log") }
}
