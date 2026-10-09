package app.kidpager.bridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony

/** Receives incoming SMS as the default SMS app and forwards each message as one `sms` event. */
class SmsReceiver : BroadcastReceiver() {
    companion object {
        private const val TAG = "sms"

        fun event(from: String, body: String, tsMillis: Long): BridgeEvent {
            val phone = PhoneNumbers.normalize(from) ?: from
            return BridgeEvent(
                id = NotificationMapper.eventId("sms:$phone", tsMillis, body).replaceFirst("n_", "s_"),
                source = Targets.SOURCE_SMS,
                conversation = Conversation(id = phone, isGroup = false),
                sender = Sender(name = phone, phone = phone),
                text = Bounds.cp(body, Bounds.TEXT_CP),
                ts = tsMillis / 1000,
            )
        }
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_DELIVER_ACTION) return
        val msgs = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
        if (msgs.isEmpty()) return
        val from = msgs[0].displayOriginatingAddress ?: msgs[0].originatingAddress ?: return
        val body = msgs.joinToString("") { it.messageBody ?: "" }
        val ts = msgs[0].timestampMillis
        val e = event(from, body, ts)
        Log.i(TAG, "in from=${Log.redact(e.sender.phone)} len=${body.length} parts=${msgs.size}")
    }
}
