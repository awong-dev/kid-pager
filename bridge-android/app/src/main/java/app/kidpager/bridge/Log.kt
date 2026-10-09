package app.kidpager.bridge

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Logs to logcat and to a ring buffer shown on the log screen. */
object Log {
    const val CAPACITY = 500
    private val fmt = SimpleDateFormat("HH:mm:ss", Locale.US)
    private val lines = ArrayDeque<String>(CAPACITY)
    private val listeners = mutableSetOf<(String) -> Unit>()

    fun i(tag: String, msg: String) = add("I", tag, msg).also { android.util.Log.i(tag, msg) }
    fun w(tag: String, msg: String) = add("W", tag, msg).also { android.util.Log.w(tag, msg) }
    fun e(tag: String, msg: String, t: Throwable? = null) {
        val full = if (t != null) "$msg: ${t.javaClass.simpleName}: ${t.message}" else msg
        add("E", tag, full)
        android.util.Log.e(tag, msg, t)
    }

    @Synchronized
    private fun add(level: String, tag: String, msg: String) {
        val line = "${fmt.format(Date())} $level/$tag: $msg"
        if (lines.size >= CAPACITY) lines.removeFirst()
        lines.addLast(line)
        listeners.toList().forEach { it(line) }
    }

    @Synchronized
    fun snapshot(): List<String> = lines.toList()

    @Synchronized
    fun addListener(l: (String) -> Unit) { listeners += l }

    @Synchronized
    fun removeListener(l: (String) -> Unit) { listeners -= l }

    /** Phone numbers are logged as their last four digits only. */
    fun redact(phone: String?): String {
        if (phone.isNullOrEmpty()) return "-"
        val digits = phone.filter { it.isDigit() }
        return "…" + digits.takeLast(4)
    }
}
