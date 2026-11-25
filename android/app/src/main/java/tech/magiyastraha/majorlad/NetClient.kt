package tech.magiyastraha.majorlad

import android.graphics.Bitmap
import android.os.Handler
import org.json.JSONArray
import org.json.JSONObject
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.UUID

/**
 * Клиент ученика: те же сообщения и то же кадрирование, что у программы
 * на компьютере — четыре байта длины и JSON в UTF-8.
 */
class NetClient(
    private val handler: Handler,
    private val myName: String,
    private val onStatus: (String) -> Unit,
    private val onChat: (String, String) -> Unit,
    private val onHistory: (List<Triple<String, String, String>>) -> Unit,
    private val onJoined: (String) -> Unit,
    private val onEnded: (String) -> Unit,
    private val onVideoState: (Boolean) -> Unit,
    private val onFrame: (Bitmap) -> Unit,
) {
    companion object {
        private const val APP_PORT = 45800
    }

    @Volatile private var running = false
    @Volatile var connected = false
        private set

    private var sock: Socket? = null
    private var input: InputStream? = null
    private var output: OutputStream? = null
    private val writeLock = Any()
    private var host = ""

    var audio: AudioEngine? = null
        private set

    fun connect(address: String) {
        disconnect()
        host = address
        running = true
        Thread({ run() }, "majorlad-net").start()
    }

    fun disconnect() {
        running = false
        connected = false
        audio?.stop()
        audio = null
        try {
            sock?.close()
        } catch (_: Exception) {
        }
        sock = null
        input = null
        output = null
    }

    fun sendChat(text: String): Boolean {
        return try {
            post { onChat(myName, text) }
            sendJson(mapOf(
                "type" to "chat",
                "id" to UUID.randomUUID().toString().substring(0, 8),
                "from" to myName,
                "text" to text))
            true
        } catch (e: Exception) {
            post { onStatus("Нет связи — сообщение не отправлено.") }
            false
        }
    }

    // -- соединение ----------------------------------------------------------

    private fun run() {
        try {
            val socket = Socket()
            socket.connect(InetSocketAddress(host, APP_PORT), 6000)
            socket.tcpNoDelay = true
            sock = socket
            input = socket.getInputStream()
            output = socket.getOutputStream()

            sendJson(mapOf("type" to "hello", "name" to myName))
            val answer = recvJson() ?: return
            when (answer.optString("type")) {
                "rejected" -> {
                    post { onStatus("Преподаватель не пустил в кабинет.") }
                    post { onEnded("rejected") }
                    finish()
                    return
                }
                "waiting", "busy" -> {
                    post { onStatus("Ждём, пока преподаватель впустит в кабинет…") }
                }
                else -> {
                    connected = true
                    startAudio()
                    post { onJoined(answer.optString("teacher", "Преподаватель")) }
                    requestHistory()
                }
            }
            readLoop()
        } catch (e: Exception) {
            if (running) {
                post { onStatus("Не удалось подключиться: ${e.message}") }
                post { onEnded("network") }
            }
            finish()
        }
    }

    private fun readLoop() {
        while (running) {
            val msg = recvJson() ?: return
            when (msg.optString("type")) {
                "ready" -> {
                    connected = true
                    startAudio()
                    post { onJoined("Преподаватель") }
                }
                "ended" -> {
                    val reason = msg.optString("reason", "lost")
                    post { onEnded(reason) }
                    finish()
                    return
                }
                "chat" -> {
                    val from = msg.optString("from", "Преподаватель")
                    val text = msg.optString("text", "")
                    if (text.isNotEmpty()) post { onChat(from, text) }
                }
                "chat_history" -> {
                    val list = ArrayList<Triple<String, String, String>>()
                    val arr: JSONArray = msg.optJSONArray("messages") ?: JSONArray()
                    for (i in 0 until arr.length()) {
                        val m = arr.optJSONObject(i) ?: continue
                        list.add(Triple(
                            m.optString("from", "Преподаватель"),
                            m.optString("text", ""),
                            m.optString("id", "")))
                    }
                    post { onHistory(list) }
                }
                "video" -> {
                    val on = msg.optBoolean("on", false)
                    post { onVideoState(on) }
                }
                "file_start" -> {
                    // Файл, присланный преподавателем: тело идёт сразу
                    // после сообщения. Пропускаем его целиком, иначе канал
                    // разъедется и дальше пойдёт мусор.
                    val size = msg.optInt("size", 0)
                    if (size > 0) {
                        var left = size
                        val buf = ByteArray(64 * 1024)
                        while (left > 0) {
                            val n = readInto(buf, minOf(left, buf.size))
                            if (n <= 0) return
                            left -= n
                        }
                    }
                }
            }
        }
    }

    private fun startAudio() {
        if (audio != null || host.isEmpty()) return
        val engine = AudioEngine(host, { bmp -> post { onFrame(bmp) } },
            { text -> post { onStatus(text) } })
        audio = engine
        engine.start()
    }

    private fun requestHistory() {
        try {
            sendJson(mapOf("type" to "chat_history_req", "before" to "",
                "limit" to 50))
        } catch (_: Exception) {
        }
    }

    private fun finish() {
        connected = false
        audio?.stop()
        audio = null
        try {
            sock?.close()
        } catch (_: Exception) {
        }
    }

    // -- кадры ---------------------------------------------------------------

    private fun sendJson(obj: Map<String, Any>) {
        val out = output ?: throw java.io.IOException("нет соединения")
        val data = JSONObject(obj).toString().toByteArray(Charsets.UTF_8)
        val head = byteArrayOf(
            ((data.size shr 24) and 0xff).toByte(),
            ((data.size shr 16) and 0xff).toByte(),
            ((data.size shr 8) and 0xff).toByte(),
            (data.size and 0xff).toByte())
        synchronized(writeLock) {
            out.write(head)
            out.write(data)
            out.flush()
        }
    }

    private fun recvJson(): JSONObject? {
        if (!running) return null
        val head = recvExact(4) ?: return null
        val size = ((head[0].toInt() and 0xff) shl 24) or
            ((head[1].toInt() and 0xff) shl 16) or
            ((head[2].toInt() and 0xff) shl 8) or
            (head[3].toInt() and 0xff)
        if (size < 0 || size > 64 * 1024 * 1024) {
            throw java.io.IOException("слишком большое сообщение")
        }
        val body = recvExact(size) ?: return null
        return JSONObject(String(body, Charsets.UTF_8))
    }

    private fun recvExact(n: Int): ByteArray? {
        val inp = input ?: return null
        val out = ByteArray(n)
        var offset = 0
        while (offset < n) {
            val read = readInto(out, n - offset, offset)
            if (read <= 0) return null
            offset += read
        }
        return out
    }

    private fun readInto(buf: ByteArray, count: Int, offset: Int = 0): Int {
        val inp = input ?: return -1
        return inp.read(buf, offset, count)
    }

    private fun post(block: () -> Unit) {
        handler.post(block)
    }
}
