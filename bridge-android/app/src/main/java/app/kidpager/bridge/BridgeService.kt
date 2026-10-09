package app.kidpager.bridge

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Foreground service that runs the outbox worker, the heartbeat timer and the event spooler.
 * Foreground type `remoteMessaging` has no 6-hour cap on API 34+.
 */
class BridgeService : Service() {
    companion object {
        private const val TAG = "service"
        const val HEARTBEAT_MS = 5L * 60 * 1000
        @Volatile var running = false
            private set

        fun start(ctx: Context) {
            val i = Intent(ctx, BridgeService::class.java)
            try {
                if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
            } catch (e: Exception) {
                // API 31+ refuses a background start unless the app is exempt from battery optimisation.
                Log.w(TAG, "cannot start from background: ${e.message}")
            }
        }

        /** `POST /bridge/heartbeat`; a non-zero `pending` in the reply triggers an outbox poll. */
        fun heartbeat(ctx: Context): Boolean {
            if (!Prefs.isPaired(ctx)) return false
            val snap = Status.gather(ctx)
            return try {
                val pending = RelayClient(ctx).heartbeat(snap.toJson(), FcmTokens.current(ctx))
                Log.i(TAG, "heartbeat ok pending=$pending battery=${snap.battery}")
                if (pending > 0) OutboxWorker.kick()
                true
            } catch (e: Exception) {
                Log.w(TAG, "heartbeat failed: ${e.message}")
                false
            }
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var outbox: OutboxWorker

    override fun onCreate() {
        super.onCreate()
        Notifications.ensureChannels(this)
        RelayClient.onUnauthorized = { Notifications.showRepair(it) }
        val n = Notifications.service(this, if (Prefs.isPaired(this)) "paired" else "not paired")
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(Notifications.ID_SERVICE, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING)
        } else {
            startForeground(Notifications.ID_SERVICE, n)
        }
        running = true
        outbox = OutboxWorker(this, scope)
        outbox.start()
        EventQueue.start(this)
        HeartbeatWorker.schedule(this)
        scope.launch {
            while (true) {
                heartbeat(this@BridgeService)
                delay(HEARTBEAT_MS)
            }
        }
        Log.i(TAG, "started paired=${Prefs.isPaired(this)}")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        running = false
        scope.cancel()
        Log.w(TAG, "destroyed")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
