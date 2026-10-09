package app.kidpager.bridge

/**
 * Best-effort phone number normaliser with a North-American default. The relay normalises
 * again with libphonenumber, so the result is a hint, not authoritative.
 */
object PhoneNumbers {
    private val digitsAndPlus = Regex("[^0-9+]")

    /** `+15551234567` for anything that looks like a phone number, else null. */
    fun normalize(raw: String?): String? {
        if (raw.isNullOrBlank()) return null
        var s = raw.trim()
        if (s.startsWith("tel:")) s = s.removePrefix("tel:")
        val cleaned = digitsAndPlus.replace(s, "")
        val plus = cleaned.startsWith("+")
        val digits = cleaned.filter { it.isDigit() }
        if (digits.length < 7 || digits.length > 15) return null
        // Any letter means it is a name, not a number.
        if (s.any { it.isLetter() }) return null
        return when {
            plus -> "+$digits"
            digits.length == 11 && digits.startsWith("1") -> "+$digits"
            digits.length == 10 -> "+1$digits"
            else -> "+$digits"
        }
    }

    /** The first E.164-looking run inside an opaque id (`t.+15551234567`, `tel:+1555...`). */
    fun extractE164(s: String?): String? {
        if (s.isNullOrBlank()) return null
        val m = Regex("\\+?[0-9][0-9 ()-]{8,20}").find(s) ?: return null
        return normalize(m.value)
    }

    fun looksLikeNumber(s: String?): Boolean = normalize(s) != null
}
