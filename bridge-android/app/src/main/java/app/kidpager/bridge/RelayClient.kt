package app.kidpager.bridge

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Client for the relay's `/bridge/...` HTTP API. Every call but `pair` carries the bearer
 * token; a 401 clears the token, so the phone must be paired again.
 */
class RelayClient(private val ctx: Context) {
    class Unauthorized : IOException("401 unauthorized")

    companion object {
        private const val TAG = "relay"
        private val JSON = "application/json; charset=utf-8".toMediaType()
        @Volatile var onUnauthorized: ((Context) -> Unit)? = null
    }

    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(20, TimeUnit.SECONDS)
        .callTimeout(60, TimeUnit.SECONDS)
        .build()

    private fun base(): String = Prefs.relayUrl(ctx)

    @Serializable

    @Serializable
    data class PairRequest(
        val code: String,
        val version: String,
        val accounts: List<String>,
        val simNumber: String? = null,
        val voiceNumber: String? = null,
        val caps: Caps,
    )

    @Serializable

    fun pair(req: PairRequest): PairResponse {
        val body = post("/bridge/pair", WireJson.encodeToString(req), auth = false, retries = 0)
    }

    fun events(events: List<BridgeEvent>): EventsResponse {
        val body = post("/bridge/events", WireJson.encodeToString(EventsRequest(events)))
        return WireJson.decodeFromString(EventsResponse.serializer(), body)
    }

    /** `title` is the only handle the UI send path has for a WhatsApp group. */
    @Serializable

    @Serializable
    data class OutboxItem(
        val id: String,
        val kind: String,
        val source: String? = null,
        val to: OutboxTo? = null,
        val text: String? = null,
        val link: String? = null,
        val msgId: String? = null,
        val bid: String? = null,
        val replyHint: String? = null,
        val sim: Int? = null,
    )

    @Serializable
    data class OutboxResponse(val items: List<OutboxItem> = emptyList())

    fun outbox(wait: Int = 0): OutboxResponse {
        val body = get("/bridge/outbox?wait=$wait")
        return WireJson.decodeFromString(OutboxResponse.serializer(), body)
    }

    @Serializable

    }

    @Serializable

    /** Returns the relay's count of pending outbox items. */
    fun heartbeat(status: JsonObject, fcmToken: String?): Int {
        val body = buildJsonObject {
            put("status", status)
            if (fcmToken != null) put("fcmToken", JsonPrimitive(fcmToken))
        }
        val text = post("/bridge/heartbeat", WireJson.encodeToString(JsonElement.serializer(), body))
        if (text.isBlank()) return 0
    }

    private fun get(path: String): String = exec(Request.Builder().url(base() + path).get(), auth = true, retries = 2)

    private fun post(path: String, json: String, auth: Boolean = true, retries: Int = 2): String =
        exec(Request.Builder().url(base() + path).post(json.toRequestBody(JSON)), auth, retries)

    private fun exec(rb: Request.Builder, auth: Boolean, retries: Int): String {
        if (auth) {
            val tok = Prefs.token(ctx) ?: throw Unauthorized()
            rb.header("Authorization", "Bearer $tok")
        }
        rb.header("User-Agent", "kidpager-bridge/${BuildConfig.VERSION_NAME}")
        var attempt = 0
        while (true) {
            try {
                http.newCall(rb.build()).execute().use { resp ->
                    val text = resp.body?.string().orEmpty()
                    when {
                        resp.isSuccessful -> return text
                        resp.code == 401 && auth -> {
                            Log.w(TAG, "401 from relay; token cleared, re-pair needed")
                            Prefs.clearToken(ctx)
                            onUnauthorized?.invoke(ctx)
                            throw Unauthorized()
                        }
                        resp.code >= 500 && attempt < retries -> Log.w(TAG, "HTTP ${resp.code}, retry ${attempt + 1}")
                        else -> throw HttpError(resp.code, text)
                    }
                }
            } catch (e: Unauthorized) {
                throw e
            } catch (e: HttpError) {
                throw e
            } catch (e: IOException) {
                if (attempt >= retries) throw e
                Log.w(TAG, "IO ${e.javaClass.simpleName}, retry ${attempt + 1}")
            }
            attempt++
            Thread.sleep(1000L shl (attempt - 1))
        }
    }
}
