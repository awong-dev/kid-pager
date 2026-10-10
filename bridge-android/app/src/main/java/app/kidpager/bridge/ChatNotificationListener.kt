package app.kidpager.bridge

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * Turns MessagingStyle notifications from Google Chat, Google Voice and WhatsApp into bridge
 * events and caches each conversation's reply action. Other notifications are ignored.
 */
class ChatNotificationListener : NotificationListenerService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    companion object {
        private const val TAG = "listener"
        @Volatile var connected = false
            private set
        /** Process-wide so it survives a listener rebind. */
        private val recent = RecentMessages()
    }

    override fun onListenerConnected() {
        connected = true
        Log.i(TAG, "connected")
        scope.launch {
            AppDb.get(this@ChatNotificationListener).seen().purgeBefore(System.currentTimeMillis() - Dedup.TTL_MS)
            rebuild()
        }
    }

    override fun onListenerDisconnected() {
        connected = false
        Log.w(TAG, "disconnected")
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (!wanted(sbn)) return
        scope.launch { handle(sbn, emitEvents = true) }
    }

    /** A listened package, and not a "N messages from M chats" group summary. */
    private fun wanted(sbn: StatusBarNotification): Boolean {
        if (sbn.packageName !in Targets.LISTEN_PACKAGES) return false
        val summary = (sbn.notification.flags and Notification.FLAG_GROUP_SUMMARY) != 0
        if (NotificationMapper.conversationKey(sbn.packageName, sbn.id, shortcutId(sbn.notification), summary) == NotificationMapper.ConversationKey.GroupSummary) {
            Log.d(TAG, "skip group summary pkg=${sbn.packageName} key=${sbn.key}")
            return false
        }
        return true
    }

    private fun shortcutId(n: Notification): String? = if (android.os.Build.VERSION.SDK_INT >= 26) n.shortcutId else null

    /** Rebuilds the reply cache from the notifications currently on the shade. */
    fun rebuild() {
        val active = try { activeNotifications } catch (e: Exception) { Log.w(TAG, "activeNotifications: ${e.message}"); return }
        ReplyCache.clear()
        var n = 0
        for (sbn in active ?: emptyArray()) {
            if (!wanted(sbn)) continue
            handle(sbn, emitEvents = true)
            n++
        }
        Log.i(TAG, "rebuilt from ${n} active notifications; ${ReplyCache.size()} reply actions")
    }

    private fun handle(sbn: StatusBarNotification, emitEvents: Boolean) {
        val snapshot = snapshot(sbn) ?: return
        val convId = NotificationMapper.conversationId(snapshot)
        if (convId == null) {
            Log.w(TAG, "no conversation id, dropping (pkg=${sbn.packageName}, id=${sbn.id}, messages=${snapshot.messages.size})")
            return
        }
        val hadAction = ReplyCache.remember(convId, sbn)
        if (!emitEvents) return
        val source = Targets.sourceFor(snapshot.pkg) ?: return
        val db = AppDb.get(this)
        val mapped = NotificationMapper.map(snapshot, seen = { db.seen().count(it) > 0 }, recentDup = { e ->
            recent.isDuplicate(snapshot.pkg, e).also { dup ->
                if (dup) Log.d(TAG, "dup message skipped conv=$convId sender=${e.sender.name} ts=${e.ts}")
            }
        })
        // Only a Voice send can arrive with just `to.phone`; a WhatsApp send carries the conversation id.
        if (source == Targets.SOURCE_GVOICE && mapped.peerPhone != null) ReplyCache.rememberPhone(source, mapped.peerPhone, convId)
        if (mapped.events.isEmpty()) return
        EventQueue.enqueue(this, mapped.events)
        val now = System.currentTimeMillis()
        for (k in mapped.dedupKeys) db.seen().insert(SeenMessage(k, now))
        Log.i(TAG, "${sbn.packageName.substringAfterLast('.')} conv=$convId +${mapped.events.size} action=${hadAction}")
    }

    /** Null for anything that is not MessagingStyle. */
    private fun snapshot(sbn: StatusBarNotification): NotificationSnapshot? {
        val n = sbn.notification
        val style = NotificationCompat.MessagingStyle.extractMessagingStyleFromNotification(n) ?: return null
        val self = style.user.name?.toString()
        val messages = style.messages.map { m ->
            val person = m.person
            SnapshotMessage(
                senderName = person?.name?.toString(),
                text = m.text?.toString(),
                timestamp = m.timestamp,
                isSelf = person == null,
            )
        }
        return NotificationSnapshot(
            pkg = sbn.packageName,
            key = sbn.key,
            id = sbn.id,
            shortcutId = shortcutId(n),
            dataUri = n.extras?.getString("android.intent.extra.TEXT")?.takeIf { it.startsWith("tel:") }
                ?: n.extras?.getCharSequence(android.app.Notification.EXTRA_SUB_TEXT)?.toString(),
            conversationTitle = style.conversationTitle?.toString(),
            isGroup = style.isGroupConversation,
            selfName = self,
            messages = messages,
        )
    }
}
