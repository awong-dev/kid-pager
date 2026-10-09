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
 */
class BridgeAccessibilityService : AccessibilityService() {
    companion object {
        private const val TAG = "a11y"
        @Volatile var instance: BridgeAccessibilityService? = null
            private set
        private val mutex = Mutex()
        private const val POLL_MS = 400L
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

    suspend fun sendViaUi(link: String, text: String): String? = mutex.withLock {
        val pkg = Targets.packageForLink(link) ?: return "no_link"
        val r: String = withTimeoutOrNull(Targets.TIER2_TIMEOUT_MS) {
            try {
                if (!open(link, pkg)) return@withTimeoutOrNull "app_missing"
                    ?: return@withTimeoutOrNull "ui_changed"
                }
            } finally {
                performGlobalAction(GLOBAL_ACTION_HOME)
            }
        } ?: "timeout"
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
