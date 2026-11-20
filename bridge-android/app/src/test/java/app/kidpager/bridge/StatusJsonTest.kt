package app.kidpager.bridge

import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class StatusJsonTest {
    private fun snap(charging: Boolean? = null, exempt: Boolean? = null, carrier: Status.Carrier? = null) = Status.Snapshot(
        battery = 80, listenerBound = true, smsDefault = true, accessibility = false, accounts = emptyList(),
        simNumber = null, voiceNumber = null, whatsappInstalled = false, version = "0.1.0", fcm = false, pollSec = 60,
        charging = charging, batteryExempt = exempt, carrier = carrier,
    )

    @Test
    fun newFieldsPresent() {
        val j = snap(true, false, Status.Carrier("310410", "AT&T", "T-Mobile")).toJson()
        assertTrue(j["charging"]!!.jsonPrimitive.boolean)
        assertFalse(j["batteryExempt"]!!.jsonPrimitive.boolean)
        val c = j["carrier"]!!.jsonObject
        assertEquals("310410", c["simOperator"]!!.jsonPrimitive.content)
        assertEquals("AT&T", c["simName"]!!.jsonPrimitive.content)
        assertEquals("T-Mobile", c["netName"]!!.jsonPrimitive.content)
    }

    @Test
    fun unknownFieldsOmitted() {
        val j = snap().toJson()
        assertNull(j["charging"]); assertNull(j["batteryExempt"]); assertNull(j["carrier"])
        val partial = snap(carrier = Status.Carrier("310410", null, null)).toJson()["carrier"]!!.jsonObject
        assertEquals(setOf("simOperator"), partial.keys)
    }
}
