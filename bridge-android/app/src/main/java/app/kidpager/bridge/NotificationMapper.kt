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

/** `peerPhone` is the DM peer's number when the source is phone-keyed (Voice, WhatsApp DM). */
data class Mapped(val events: List<BridgeEvent>, val dedupKeys: List<String>, val peerPhone: String?)

object NotificationMapper {
    /** WhatsApp posts the bridge account's own group lines as sender "You". */
    private const val WA_SELF = "You"

    sealed class ConversationKey {
        data class Id(val id: String) : ConversationKey()
        object GroupSummary : ConversationKey()
        /** A roll-up notification: no `shortcutId`, and the package gets no `pkg|id` fallback. */
        object NoId : ConversationKey()
    }

    /**
     * Google Chat posts roll-ups of recent messages with no `shortcutId` and id 0, so only
     * [Targets.PKG_ID_FALLBACK_PACKAGES] may fall back to `pkg|id` as the conversation id.
     */
    fun conversationKey(pkg: String, id: Int, shortcutId: String?, groupSummary: Boolean): ConversationKey {
        if (groupSummary) return ConversationKey.GroupSummary
        shortcutId?.takeIf { it.isNotBlank() }?.let { return ConversationKey.Id(it) }
        return if (pkg in Targets.PKG_ID_FALLBACK_PACKAGES) ConversationKey.Id("$pkg|$id") else ConversationKey.NoId
    }

    /** Null means drop the notification. */
    fun conversationId(s: NotificationSnapshot): String? =
        (conversationKey(s.pkg, s.id, s.shortcutId, groupSummary = false) as? ConversationKey.Id)?.id

    /** One event per message that neither `seen` (by dedup key) nor `recentDup` reports. */
    fun map(
        s: NotificationSnapshot,
        seen: (String) -> Boolean,
        nowSec: Long = System.currentTimeMillis() / 1000,
        recentDup: (BridgeEvent) -> Boolean = { false },
    ): Mapped {
        val source = Targets.sourceFor(s.pkg) ?: return Mapped(emptyList(), emptyList(), null)
        val convId = conversationId(s) ?: return Mapped(emptyList(), emptyList(), null)
        val phoneKeyed = source == Targets.SOURCE_GVOICE || (source == Targets.SOURCE_WHATSAPP && !s.isGroup)
        val phoneFromIds = when {
            source == Targets.SOURCE_GVOICE -> PhoneNumbers.extractE164(s.shortcutId) ?: PhoneNumbers.extractE164(s.dataUri)
            // A WhatsApp `<digits>@s.whatsapp.net` JID carries the number; `<digits>@lid` does not.
            phoneKeyed -> Targets.phoneFromWaJid(s.shortcutId) ?: PhoneNumbers.extractE164(s.dataUri)
            else -> null
        }
        val phoneFromTitle = if (phoneKeyed) PhoneNumbers.normalize(s.conversationTitle) else null
        // WhatsApp has a deep link only for a phone-JID DM. Google Chat's shortcutId is the id in
        val events = ArrayList<BridgeEvent>()
        val keys = ArrayList<String>()
        var peerPhone: String? = phoneFromIds ?: phoneFromTitle
        for (m in s.messages) {
            val raw = m.text?.toString()?.trim().orEmpty()
            if (raw.isEmpty()) continue
            if (m.isSelf) continue
            var name = m.senderName?.trim().orEmpty()
            if (name.isNotEmpty() && s.selfName != null && name == s.selfName.trim()) continue
            var text = raw
            var attachments: List<Attachment>? = null
            if (source == Targets.SOURCE_WHATSAPP) {
                name = stripWaTilde(name)
                if (name == WA_SELF) continue
                waMedia(raw)?.let { (kind, caption) -> attachments = listOf(Attachment(kind)); text = caption }
            }
            val key = Dedup.key(convId, m.timestamp, raw)
            if (seen(key)) continue
            var phone: String? = null
            if (phoneKeyed) {
                // Voice and WhatsApp show the peer's number as the sender or title when it is not a contact.
                phone = phoneFromIds ?: PhoneNumbers.normalize(name) ?: phoneFromTitle
                if (phone != null) peerPhone = phone
            }
            val tsSec = if (m.timestamp > 0) m.timestamp / 1000 else nowSec
            val event = BridgeEvent(
                id = eventId(convId, m.timestamp, raw),
                source = source,
                conversation = Conversation(id = Bounds.convId(convId), title = s.conversationTitle?.let { Bounds.cp(it, Bounds.NAME_CP) }, isGroup = s.isGroup, link = link),
                sender = Sender(name = Bounds.cp(name.ifEmpty { s.conversationTitle ?: "?" }, Bounds.NAME_CP), phone = phone),
                text = Bounds.cp(text, Bounds.TEXT_CP),
                ts = tsSec,
                attachments = attachments,
            )
            if (recentDup(event)) continue
            events += event
            keys += key
        }
        return Mapped(events, keys, peerPhone)
    }

    /** WhatsApp shows a group member who is not a saved contact as `~ Name`. */
    fun stripWaTilde(name: String): String =
        if (name.startsWith("~")) name.removePrefix("~").trim() else name

    /** (attachment kind, caption) for a WhatsApp media placeholder line such as `📷 Photo`, else null. */
    fun waMedia(text: String): Pair<String, String>? {
        val t = text.trim()
        for (m in Targets.WA_MEDIA) {
            if (m.label == null) {
                if (t.equals(m.prefix, ignoreCase = true)) return m.kind to ""
                continue
            }
            if (!t.startsWith(m.prefix)) continue
            val rest = t.removePrefix(m.prefix).trim()
            return m.kind to (if (rest.isEmpty() || rest.equals(m.label, ignoreCase = true)) "" else rest)
        }
        return null
    }

    /** Stable for the same message, so the relay can reject a retried event as a duplicate. */
    fun eventId(convId: String, timestamp: Long, text: String): String {
        val digest = java.security.MessageDigest.getInstance("SHA-256")
            .digest("$convId|$timestamp|$text".toByteArray())
        return "n_" + digest.take(12).joinToString("") { "%02x".format(it) }
    }
}

/**
 * Bounded window of recently spooled messages, keyed without the conversation id, that catches
 * a message an app posts again under another notification key.
 */
class RecentMessages(private val ttlMs: Long = TTL_MS, private val maxEntries: Int = MAX_ENTRIES) {
    companion object {
        const val TTL_MS = 5L * 60 * 1000
        const val MAX_ENTRIES = 256
    }

    data class Key(val pkg: String, val sender: String, val text: String, val ts: Long)

    private val seen = LinkedHashMap<Key, Long>() // insertion order = eviction order

    /** Records `key` when it is not a duplicate. */
    @Synchronized
    fun isDuplicate(key: Key, now: Long = System.currentTimeMillis()): Boolean {
        val it = seen.entries.iterator()
        while (it.hasNext()) {
            if (now - it.next().value >= ttlMs) it.remove() else break
        }
        if (seen.containsKey(key)) return true
        seen[key] = now
        while (seen.size > maxEntries) seen.remove(seen.keys.first())
        return false
    }

    fun isDuplicate(pkg: String, e: BridgeEvent, now: Long = System.currentTimeMillis()): Boolean =
        isDuplicate(Key(pkg, e.sender.name, e.text, e.ts), now)

    @Synchronized
    fun size(): Int = seen.size
}
