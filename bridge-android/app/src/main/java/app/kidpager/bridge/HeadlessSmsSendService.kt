package app.kidpager.bridge

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.telephony.TelephonyManager

/** Android grants the default-SMS role only to an app with a RESPOND_VIA_MESSAGE service. */
class HeadlessSmsSendService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == TelephonyManager.ACTION_RESPOND_VIA_MESSAGE) {
            val to = intent.data?.schemeSpecificPart
            val text = intent.getStringExtra(Intent.EXTRA_TEXT)
            if (to != null && !text.isNullOrBlank()) {
            }
        }
        stopSelf(startId)
        return START_NOT_STICKY
    }
}
