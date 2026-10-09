package app.kidpager.bridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restarts the bridge service after boot and after an app update. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        Log.i("boot", "${intent.action}; starting service")
        BridgeService.start(context)
    }
}
