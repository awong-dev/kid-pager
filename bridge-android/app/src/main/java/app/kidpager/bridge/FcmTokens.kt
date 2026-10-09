package app.kidpager.bridge

import android.content.Context

/** The FCM registration token for the heartbeat body; null in a build without Firebase. */
object FcmTokens {
    fun current(ctx: Context): String? = if (BuildConfig.FCM) Prefs.fcmToken(ctx) else null
}
