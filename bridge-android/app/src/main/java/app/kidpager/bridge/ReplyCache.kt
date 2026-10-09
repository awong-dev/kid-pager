package app.kidpager.bridge

import android.app.Notification
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.service.notification.StatusBarNotification
import androidx.core.app.NotificationCompat

/**
 * Tier 1 send: the reply action (PendingIntent and RemoteInputs) of each conversation's latest
 * notification, fired to answer in that conversation without opening the app.
 */
object ReplyCache {
    private const val TAG = "reply"

    class Entry(val pkg: String, val label: String, private val fire: (Context, String) -> Unit) {
        fun send(ctx: Context, text: String) = fire(ctx, text)
    }

    private val byConversation = HashMap<String, Entry>()

    @Synchronized fun size() = byConversation.size

    @Synchronized

    @Synchronized

    @Synchronized

    @Synchronized
    fun has(conversationId: String) = byConversation.containsKey(conversationId)

    /** True when the notification has a reply action. */
    @Synchronized
    fun remember(conversationId: String, sbn: StatusBarNotification): Boolean {
        val entry = extract(sbn) ?: return false
        byConversation[conversationId] = entry
        return true
    }

    private fun extract(sbn: StatusBarNotification): Entry? {
        val n = sbn.notification
        n.actions?.forEach { a ->
            val ris = a.remoteInputs
            if (!ris.isNullOrEmpty() && a.actionIntent != null) {
                return Entry(sbn.packageName, a.title?.toString() ?: "reply") { ctx, text ->
                    val intent = Intent()
                    val results = Bundle()
                    for (ri in ris) results.putCharSequence(ri.resultKey, text)
                    android.app.RemoteInput.addResultsToIntent(ris, intent, results)
                    a.actionIntent.send(ctx, 0, intent)
                }
            }
        }
        NotificationCompat.WearableExtender(n).actions.forEach { a ->
            val ris = a.remoteInputs
            if (!ris.isNullOrEmpty() && a.actionIntent != null) {
                return Entry(sbn.packageName, a.title?.toString() ?: "reply") { ctx, text ->
                    val intent = Intent()
                    val results = Bundle()
                    for (ri in ris) results.putCharSequence(ri.resultKey, text)
                    androidx.core.app.RemoteInput.addResultsToIntent(ris, intent, results)
                    a.actionIntent!!.send(ctx, 0, intent)
                }
            }
        }
        return null
    }

    /** False when there is no cached action or the app has cancelled its PendingIntent. */
    fun reply(ctx: Context, conversationId: String, text: String): Boolean {
        val entry = synchronized(this) { byConversation[conversationId] } ?: run {
            Log.i(TAG, "no cached action for $conversationId")
            return false
        }
        return try {
            entry.send(ctx, text)
            Log.i(TAG, "tier1 sent conv=$conversationId via ${entry.pkg}")
            true
        } catch (e: PendingIntent.CanceledException) {
            Log.w(TAG, "tier1 cancelled conv=$conversationId; dropping action")
            synchronized(this) { byConversation.remove(conversationId) }
            false
        }
    }

    @Synchronized
    fun describe(): String = byConversation.entries.joinToString("\n") { "${it.key} -> ${it.value.pkg} [${it.value.label}]" }
}
