package app.kidpager.bridge

import android.app.Service
import android.content.Intent
import android.os.IBinder

/** Build without Firebase: a placeholder so the manifest's FcmService entry resolves. */
class FcmService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null
}
