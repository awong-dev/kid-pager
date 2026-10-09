package app.kidpager.bridge

import java.net.URLEncoder

/**
 */
object Targets {
    const val GCHAT_PKG = "com.google.android.apps.dynamite"
    const val GVOICE_PKG = "com.google.android.apps.googlevoice"


    const val SOURCE_SMS = "sms"
    const val SOURCE_GCHAT = "gchat"
    const val SOURCE_GVOICE = "gvoice"

    fun sourceFor(pkg: String): String? = when (pkg) {
        GCHAT_PKG -> SOURCE_GCHAT
        GVOICE_PKG -> SOURCE_GVOICE
        else -> null
    }

    val CHAT_LINK = Regex("^https://(chat\\.google\\.com|mail\\.google\\.com/chat)(/|$)")
    val VOICE_LINK = Regex("^https://voice\\.google\\.com(/|$)")

    /** Null when the link belongs to none of the bridged apps. */
    fun packageForLink(link: String): String? = when {
        CHAT_LINK.containsMatchIn(link) -> GCHAT_PKG
        VOICE_LINK.containsMatchIn(link) -> GVOICE_PKG
        else -> null
    }

    fun voiceThreadLink(e164: String): String =
        "https://voice.google.com/u/0/messages?itemId=t." + URLEncoder.encode(e164, "UTF-8")

    /** The id the app's own notifications use for the conversation a link opens. */
    fun conversationIdFromLink(link: String): String? {
        Regex("[?&]itemId=([^&#]+)").find(link)?.let { return java.net.URLDecoder.decode(it.groupValues[1], "UTF-8") }
        return null
    }

    /** Google Chat's composer is an `EditText` with id `compose_rich_edit_text`. */
    const val COMPOSER_CLASS_HINT = "EditText"
    val SENDER_ID_HINTS = listOf("sender", "author", "user_name", "display_name", "contact_name")

    const val OPEN_TIMEOUT_MS = 15_000L
    const val VERIFY_TIMEOUT_MS = 5_000L
    const val TIER2_TIMEOUT_MS = 20_000L
}
