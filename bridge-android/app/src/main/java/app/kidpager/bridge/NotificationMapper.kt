package app.kidpager.bridge

/** Maps a MessagingStyle notification, flattened into a [NotificationSnapshot], to events. */
data class SnapshotMessage(val senderName: String?, val text: String?, val timestamp: Long, val isSelf: Boolean)

data class NotificationSnapshot(
    val pkg: String,
    val key: String, // `user|pkg|id|tag|uid`
    val id: Int,
    val shortcutId: String?,
    val dataUri: String? = null, // Voice: `tel:` URI or thread id
    val conversationTitle: String?,
    val isGroup: Boolean,
    val selfName: String?, // `MessagingStyle.user.name`
    val messages: List<SnapshotMessage>,
)


object NotificationMapper {
    /**
     */

    /** One event per message that neither `seen` (by dedup key) nor `recentDup` reports. */
        val source = Targets.sourceFor(s.pkg) ?: return Mapped(emptyList(), emptyList(), null)
        val events = ArrayList<BridgeEvent>()
        val keys = ArrayList<String>()
        for (m in s.messages) {
            if (m.isSelf) continue
            if (name.isNotEmpty() && s.selfName != null && name == s.selfName.trim()) continue
            if (seen(key)) continue
            var phone: String? = null
                if (phone != null) peerPhone = phone
            }
            val tsSec = if (m.timestamp > 0) m.timestamp / 1000 else nowSec
                source = source,
                sender = Sender(name = Bounds.cp(name.ifEmpty { s.conversationTitle ?: "?" }, Bounds.NAME_CP), phone = phone),
                text = Bounds.cp(text, Bounds.TEXT_CP),
                ts = tsSec,
            )
            keys += key
        }
        return Mapped(events, keys, peerPhone)
    }

    /** Stable for the same message, so the relay can reject a retried event as a duplicate. */
    fun eventId(convId: String, timestamp: Long, text: String): String {
        val digest = java.security.MessageDigest.getInstance("SHA-256")
            .digest("$convId|$timestamp|$text".toByteArray())
        return "n_" + digest.take(12).joinToString("") { "%02x".format(it) }
    }
}
