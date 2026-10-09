package app.kidpager.bridge

import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/** Build with Firebase: a `{kind: "outbox"}` data push triggers an outbox poll. */
class FcmService : FirebaseMessagingService() {
    override fun onMessageReceived(message: RemoteMessage) {
        if (message.data["kind"] == "outbox") {
            Log.i("fcm", "outbox push")
            if (!BridgeService.running) BridgeService.start(this)
            OutboxWorker.kick()
        }
    }

    override fun onNewToken(token: String) {
        Prefs.setFcmToken(this, token)
        Log.i("fcm", "new token")
        Thread { BridgeService.heartbeat(this) }.start()
    }
}
