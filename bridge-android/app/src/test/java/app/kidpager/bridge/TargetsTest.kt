package app.kidpager.bridge

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TargetsTest {
    @Test
    fun whatsappPackagesAreListenedAndMapped() {
        assertTrue(Targets.WHATSAPP_PKG in Targets.LISTEN_PACKAGES)
        assertTrue(Targets.WHATSAPP_BUSINESS_PKG in Targets.LISTEN_PACKAGES)
        assertEquals("whatsapp", Targets.sourceFor(Targets.WHATSAPP_PKG))
        assertEquals("whatsapp", Targets.sourceFor(Targets.WHATSAPP_BUSINESS_PKG))
        assertEquals("gchat", Targets.sourceFor(Targets.GCHAT_PKG))
        assertNull(Targets.sourceFor("com.example"))
    }

    @Test
    fun whatsappLinksResolveToThePackage() {
        assertEquals(Targets.WHATSAPP_PKG, Targets.packageForLink("https://wa.me/15551234567"))
        assertEquals(Targets.WHATSAPP_PKG, Targets.packageForLink("https://api.whatsapp.com/send?phone=15551234567&text=hi"))
        assertEquals(Targets.WHATSAPP_PKG, Targets.packageForLink("whatsapp://send?phone=15551234567"))
        assertNull(Targets.packageForLink("https://chat.whatsapp.com/AbCdEf"))
        assertNull(Targets.packageForLink("https://wa.me.example.com/x"))
        assertEquals(Targets.GVOICE_PKG, Targets.packageForLink("https://voice.google.com/u/0/messages"))
    }

    @Test
    fun waLinkIsDigitsOnly() {
        assertEquals("https://wa.me/15551234567", Targets.waLink("+15551234567"))
        assertEquals("https://wa.me/15551234567", Targets.waLink("+1 (555) 123-4567"))
    }

    @Test
    fun conversationIdFromWhatsappLinkIsTheDmJid() {
        assertEquals("15551234567@s.whatsapp.net", Targets.conversationIdFromLink("https://wa.me/15551234567"))
        assertEquals("15551234567@s.whatsapp.net", Targets.conversationIdFromLink("https://wa.me/+15551234567?text=hi"))
        assertEquals("15551234567@s.whatsapp.net", Targets.conversationIdFromLink("https://api.whatsapp.com/send?phone=15551234567"))
        assertEquals("15551234567@s.whatsapp.net", Targets.conversationIdFromLink("https://api.whatsapp.com/send?text=hi&phone=%2B15551234567"))
        assertEquals("15551234567@s.whatsapp.net", Targets.conversationIdFromLink("whatsapp://send?phone=15551234567"))
        assertNull(Targets.conversationIdFromLink("https://wa.me/"))
        assertEquals("t.+15551234567", Targets.conversationIdFromLink(Targets.voiceThreadLink("+15551234567")))
        assertEquals("AAAAxyz", Targets.conversationIdFromLink("https://chat.google.com/room/AAAAxyz/abc"))
    }

    @Test
    fun chatLinkIsDmOrRoomOnChatGoogleCom() {
        assertEquals("https://chat.google.com/dm/DMexample01", Targets.chatLink("DMexample01", isGroup = false))
        assertEquals("https://chat.google.com/room/ROOMexample", Targets.chatLink("ROOMexample", isGroup = true))
        assertEquals(Targets.GCHAT_PKG, Targets.packageForLink(Targets.chatLink("DMexample01", isGroup = false)))
    }

    @Test
    fun conversationIdFromChatLinkIsTheBareShortcutId() {
        // The id from a pasted link must equal the notification shortcutId.
        assertEquals("DMexample01", Targets.conversationIdFromLink("https://chat.google.com/dm/DMexample01"))
        assertEquals("ROOMexample", Targets.conversationIdFromLink("https://chat.google.com/room/ROOMexample"))
        assertEquals("ROOMexample", Targets.conversationIdFromLink("https://chat.google.com/room/ROOMexample/abc?x=1"))
        assertEquals("SSSS", Targets.conversationIdFromLink("https://chat.google.com/space/SSSS"))
        assertEquals("BBBB", Targets.conversationIdFromLink("https://mail.google.com/chat/u/0/#chat/dm/BBBB"))
        assertEquals("DMexample01", Targets.conversationIdFromLink(Targets.chatLink("DMexample01", isGroup = false)))
        assertEquals("ROOMexample", Targets.conversationIdFromLink(Targets.chatLink("ROOMexample", isGroup = true)))
        assertNull(Targets.conversationIdFromLink("https://chat.google.com/"))
    }

    @Test
    fun sendDescriptionMatchesChatPostMessage() {
        for (ok in listOf("Post message", "post message", "Post", "Send", "Send message", "SEND")) {
            assertTrue(ok, Targets.SEND_DESCRIPTION.matches(ok))
        }
        for (bad in listOf("Post a message", "Resend", "Send to", "Message", "")) {
            assertFalse(bad, Targets.SEND_DESCRIPTION.matches(bad))
        }
    }

    @Test
    fun chatSelectorsMatchByResourceId() {
        val chat = "com.google.android.apps.dynamite:id/"
        assertTrue(Targets.idMatches(chat + "post_message_button", Targets.SEND_ID_HINTS))
        assertTrue(Targets.idMatches("com.whatsapp:id/send_button", Targets.SEND_ID_HINTS))
        assertTrue(Targets.idMatches(chat + "action_bar_title", Targets.TITLE_ID_HINTS))
        assertTrue(Targets.idMatches(chat + "user_name", Targets.SENDER_ID_HINTS))
        assertFalse(Targets.idMatches(chat + "compose_rich_edit_text", Targets.SEND_ID_HINTS))
        assertFalse(Targets.idMatches(chat + "message_text", Targets.SENDER_ID_HINTS))
        assertFalse(Targets.idMatches(null, Targets.SEND_ID_HINTS))
        assertFalse(Targets.idMatches("", Targets.SEND_ID_HINTS))
    }
}
