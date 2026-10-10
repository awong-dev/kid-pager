package app.kidpager.bridge

import android.accessibilityservice.AccessibilityService
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Tier 2 send: opens a conversation in the chat app's own UI, types the text, taps Send and
 * checks that the text appears in the thread. Also reads a conversation's title and senders.
 */
class BridgeAccessibilityService : AccessibilityService() {
    companion object {
        private const val TAG = "a11y"
        @Volatile var instance: BridgeAccessibilityService? = null
            private set
        private val mutex = Mutex()
        private const val POLL_MS = 400L
        private const val SEARCH_TIER2_TIMEOUT_MS = 30_000L
    }

    data class Inspected(val conversationId: String, val title: String?, val isGroup: Boolean, val people: List<String>)

    override fun onServiceConnected() {
        instance = this
        serviceInfo = serviceInfo.apply { packageNames = Targets.LISTEN_PACKAGES.toTypedArray() }
        Log.i(TAG, "connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}
    override fun onDestroy() { instance = null; super.onDestroy() }

    /** Null on success, else the ack reason (`no_link`, `app_missing`, `ui_changed`, `unverified`, `timeout`). */
    suspend fun sendViaUi(link: String, text: String): String? = mutex.withLock {
        val pkg = Targets.packageForLink(link) ?: return "no_link"
        val r: String = withTimeoutOrNull(Targets.TIER2_TIMEOUT_MS) {
            try {
                if (!open(link, pkg)) return@withTimeoutOrNull "app_missing"
            } finally {
                performGlobalAction(GLOBAL_ACTION_HOME)
            }
        } ?: "timeout"
        Log.i(TAG, "tier2 send $r pkg=${pkg.substringAfterLast('.')}")
        return if (r == "ok") null else r
    }

    /** Finds a chat with no deep link (a WhatsApp group) through the chat-list search, then sends. */
    suspend fun sendViaSearch(pkg: String, title: String, text: String): String? = mutex.withLock {
        val want = title.trim()
        val r: String = withTimeoutOrNull(SEARCH_TIER2_TIMEOUT_MS) {
            try {
                val launch = packageManager.getLaunchIntentForPackage(pkg) ?: return@withTimeoutOrNull "app_missing"
                startActivity(launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP))
                waitFor(Targets.OPEN_TIMEOUT_MS) { root -> root.takeIf { it.packageName == pkg } }
                    ?: return@withTimeoutOrNull "ui_changed"
                delay(500)
                val search = waitFor(3_000) { root -> findFirst(root) { isWaSearch(it) } }
                    ?: run { Log.w(TAG, "search button not found"); return@withTimeoutOrNull "ui_changed" }
                if (!clickUp(search)) { Log.w(TAG, "search not clickable"); return@withTimeoutOrNull "ui_changed" }
                val field = waitFor(3_000) { root -> findFirst(root) { isComposer(it) } }
                    ?: run { Log.w(TAG, "search field not found"); return@withTimeoutOrNull "ui_changed" }
                if (!setText(field, want)) return@withTimeoutOrNull "ui_changed"
                val row = waitFor(Targets.SEARCH_TIMEOUT_MS) { root ->
                    findFirst(root) { isWaRow(it, want) && idHas(it, Targets.WA_ROW_NAME_ID_HINTS) }
                        ?: findFirst(root) { isWaRow(it, want) }
                } ?: run { Log.w(TAG, "no chat row matches the title"); return@withTimeoutOrNull "no_match" }
                if (!clickUp(row)) { Log.w(TAG, "row not clickable"); return@withTimeoutOrNull "ui_changed" }
                // Excludes the search box, which is also editable and still holds the title.
                composeAndSend(text) { n ->
                    isComposer(n) && (idHas(n, Targets.WA_COMPOSER_ID_HINTS) || n.text?.toString()?.trim() != want)
                }
            } finally {
                performGlobalAction(GLOBAL_ACTION_HOME)
            }
        } ?: "timeout"
        Log.i(TAG, "tier2 search-send $r pkg=${pkg.substringAfterLast('.')}")
        return if (r == "ok") null else r
    }

    /** Null when the conversation did not open. */
    suspend fun inspect(link: String): Inspected? = mutex.withLock {
        val pkg = Targets.packageForLink(link) ?: return null
        val convId = Targets.conversationIdFromLink(link) ?: link
        withTimeoutOrNull(Targets.TIER2_TIMEOUT_MS) {
            try {
                if (!open(link, pkg)) return@withTimeoutOrNull null
                waitFor(Targets.OPEN_TIMEOUT_MS) { root -> root.takeIf { it.packageName == pkg } } ?: return@withTimeoutOrNull null
                val root = rootInActiveWindow ?: return@withTimeoutOrNull null
                val title = findFirst(root) { n -> idHas(n, Targets.TITLE_ID_HINTS) && !n.text.isNullOrBlank() }?.text?.toString()
                    ?: root.window?.title?.toString()
                val people = LinkedHashSet<String>()
                collect(root) { n -> idHas(n, Targets.SENDER_ID_HINTS) && !n.text.isNullOrBlank() }
                    .forEach { people += it.text.toString().trim() }
                    else -> people.size > 1
                }
                Inspected(convId, title, isGroup, people.take(64))
            } finally {
                performGlobalAction(GLOBAL_ACTION_HOME)
            }
        }.also { Log.i(TAG, "inspect conv=$convId title=${it?.title ?: "-"} people=${it?.people?.size ?: 0}") }
    }

    /** Returns "ok" or an ack reason. */
        delay(300)
        val needle = text.take(60)
        val shown = waitFor(Targets.VERIFY_TIMEOUT_MS) { root ->
            findFirst(root) { !it.isEditable && it.text?.toString()?.contains(needle) == true }
        }
        return if (shown == null) "unverified" else "ok"
    }

    private fun setText(node: AccessibilityNodeInfo, text: String): Boolean {
        val args = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text) }
        if (node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) return true
        Log.w(TAG, "set text refused")
        return false
    }

    private fun open(link: String, pkg: String): Boolean {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(link))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        // WhatsApp Business handles the same links when the consumer app is absent.
        val candidates = if (pkg == Targets.WHATSAPP_PKG) listOf(pkg, Targets.WHATSAPP_BUSINESS_PKG) else listOf(pkg)
        for (p in candidates) {
            try {
                startActivity(Intent(intent).setPackage(p))
                return true
            } catch (e: ActivityNotFoundException) {
                Log.w(TAG, "no activity for $p")
            }
        }
        return false
    }

    private suspend fun <T> waitFor(ms: Long, probe: (AccessibilityNodeInfo) -> T?): T? {
        val end = System.currentTimeMillis() + ms
        while (System.currentTimeMillis() < end) {
            rootInActiveWindow?.let { root -> probe(root)?.let { return it } }
            delay(POLL_MS)
        }
        return null
    }

    private fun isComposer(n: AccessibilityNodeInfo): Boolean =
        n.isVisibleToUser && (n.isEditable || n.className?.toString()?.contains(Targets.COMPOSER_CLASS_HINT) == true)

    private fun isSendButton(n: AccessibilityNodeInfo): Boolean {
        if (!n.isVisibleToUser) return false
        val desc = n.contentDescription?.toString()?.trim()
        val txt = n.text?.toString()?.trim()
    }

    private fun isWaSearch(n: AccessibilityNodeInfo): Boolean {
        if (!n.isVisibleToUser || n.isEditable) return false
        val desc = n.contentDescription?.toString()?.trim()
        return idHas(n, Targets.WA_SEARCH_ID_HINTS) || (desc != null && Targets.WA_SEARCH_DESC.matches(desc))
    }

    private fun isWaRow(n: AccessibilityNodeInfo, want: String): Boolean =
        n.isVisibleToUser && !n.isEditable && n.text?.toString()?.trim().equals(want, ignoreCase = true)


    private fun clickUp(n: AccessibilityNodeInfo): Boolean {
        var cur: AccessibilityNodeInfo? = n
        var depth = 0
        while (cur != null && depth < 5) {
            if (cur.isClickable && cur.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
            cur = cur.parent
            depth++
        }
        return false
    }

    private fun findFirst(root: AccessibilityNodeInfo, pred: (AccessibilityNodeInfo) -> Boolean): AccessibilityNodeInfo? {
        if (pred(root)) return root
        for (i in 0 until root.childCount) {
            val c = root.getChild(i) ?: continue
            findFirst(c, pred)?.let { return it }
        }
        return null
    }

    private fun collect(root: AccessibilityNodeInfo, pred: (AccessibilityNodeInfo) -> Boolean): List<AccessibilityNodeInfo> {
        val out = ArrayList<AccessibilityNodeInfo>()
        fun walk(n: AccessibilityNodeInfo) {
            if (pred(n)) out += n
            for (i in 0 until n.childCount) n.getChild(i)?.let { walk(it) }
        }
        walk(root)
        return out
    }
}
