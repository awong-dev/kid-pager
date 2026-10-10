package app.kidpager.bridge

import java.net.URLEncoder

/**
 * Every string that depends on a chat app's current version: package names, deep-link patterns
 * and the accessibility selectors.
 */
object Targets {
    const val GCHAT_PKG = "com.google.android.apps.dynamite"
    const val GVOICE_PKG = "com.google.android.apps.googlevoice"
    const val WHATSAPP_PKG = "com.whatsapp"
    const val WHATSAPP_BUSINESS_PKG = "com.whatsapp.w4b"

    val LISTEN_PACKAGES = setOf(GCHAT_PKG, GVOICE_PKG, WHATSAPP_PKG, WHATSAPP_BUSINESS_PKG)

    /**
     * Packages whose notifications are keyed `pkg|id` when they carry no `shortcutId`. Chat and
     * WhatsApp set `shortcutId` on every real conversation, so they are not listed.
     */
    val PKG_ID_FALLBACK_PACKAGES = setOf(GVOICE_PKG)

    const val SOURCE_SMS = "sms"
    const val SOURCE_GCHAT = "gchat"
    const val SOURCE_GVOICE = "gvoice"
    const val SOURCE_WHATSAPP = "whatsapp"

    fun sourceFor(pkg: String): String? = when (pkg) {
        GCHAT_PKG -> SOURCE_GCHAT
        GVOICE_PKG -> SOURCE_GVOICE
        WHATSAPP_PKG, WHATSAPP_BUSINESS_PKG -> SOURCE_WHATSAPP
        else -> null
    }

    val CHAT_LINK = Regex("^https://(chat\\.google\\.com|mail\\.google\\.com/chat)(/|$)")
    val VOICE_LINK = Regex("^https://voice\\.google\\.com(/|$)")
    val WA_LINK = Regex("^(https://wa\\.me/|https://api\\.whatsapp\\.com/send\\?|whatsapp://send\\?)", RegexOption.IGNORE_CASE)

    /** Null when the link belongs to none of the bridged apps. */
    fun packageForLink(link: String): String? = when {
        CHAT_LINK.containsMatchIn(link) -> GCHAT_PKG
        VOICE_LINK.containsMatchIn(link) -> GVOICE_PKG
        WA_LINK.containsMatchIn(link) -> WHATSAPP_PKG
        else -> null
    }

    fun voiceThreadLink(e164: String): String =
        "https://voice.google.com/u/0/messages?itemId=t." + URLEncoder.encode(e164, "UTF-8")

    fun waLink(e164: String): String = "https://wa.me/" + e164.filter { it.isDigit() }

    /** WhatsApp notification `shortcutId`s: a DM by phone number, a group, a DM by privacy id. */
    val WA_DM_JID = Regex("^(\\d{6,15})@s\\.whatsapp\\.net$")
    val WA_GROUP_JID = Regex("^[\\d-]+@g\\.us$")
    val WA_LID_JID = Regex("^\\d+@lid$")

    /** `+<digits>` from a DM JID, null for anything else. */
    fun phoneFromWaJid(jid: String?): String? = jid?.let { WA_DM_JID.find(it.trim())?.groupValues?.get(1) }?.let { "+$it" }

    fun waDmJid(e164: String): String = e164.filter { it.isDigit() } + "@s.whatsapp.net"

    /** The id the app's own notifications use for the conversation a link opens. */
    fun conversationIdFromLink(link: String): String? {
        if (WA_LINK.containsMatchIn(link)) {
            val digits = Regex("^https://wa\\.me/\\+?(\\d{6,15})", RegexOption.IGNORE_CASE).find(link)?.groupValues?.get(1)
                ?: Regex("[?&]phone=\\+?(?:%2B)?(\\d{6,15})", RegexOption.IGNORE_CASE).find(link)?.groupValues?.get(1)
                ?: return null
            return waDmJid(digits)
        }
        Regex("[?&]itemId=([^&#]+)").find(link)?.let { return java.net.URLDecoder.decode(it.groupValues[1], "UTF-8") }
        return null
    }

    /** Google Chat's composer is an `EditText` with id `compose_rich_edit_text`. */
    const val COMPOSER_CLASS_HINT = "EditText"
    val SENDER_ID_HINTS = listOf("sender", "author", "user_name", "display_name", "contact_name")

    val WA_SEARCH_ID_HINTS = listOf("menuitem_search", "search")
    val WA_SEARCH_DESC = Regex("^search$", RegexOption.IGNORE_CASE)
    val WA_ROW_NAME_ID_HINTS = listOf("conversations_row_contact_name", "conversation_contact_name", "contact_name")
    val WA_COMPOSER_ID_HINTS = listOf("entry")

    /** Media placeholders in WhatsApp notification text (English locale). */
    val WA_MEDIA = listOf(
        WaMedia("📷", "Photo", "image"),
        WaMedia("🎥", "Video", "video"),
        WaMedia("🎤", "Voice message", "audio"),
        WaMedia("🎵", "Audio", "audio"),
        WaMedia("📄", "Document", "file"),
        WaMedia("📍", "Location", "file"),
        WaMedia("👤", "Contact", "file"),
        WaMedia("GIF", null, "image"),
        WaMedia("Sticker", null, "image"),
    )

    /** `prefix label` is a bare placeholder; any other text after `prefix` is a caption. */
    data class WaMedia(val prefix: String, val label: String?, val kind: String)

    const val OPEN_TIMEOUT_MS = 15_000L
    const val VERIFY_TIMEOUT_MS = 5_000L
    const val TIER2_TIMEOUT_MS = 20_000L
    const val SEARCH_TIMEOUT_MS = 5_000L
}
