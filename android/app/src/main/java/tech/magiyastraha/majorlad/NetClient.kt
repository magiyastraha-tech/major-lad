package tech.magiyastraha.majorlad

import android.graphics.Bitmap
import android.os.Handler
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Ученик подключается к преподавателю: обмен сообщениями, файлами,
 * видео и метрономом.
 */
class NetClient(
    private val handler: Handler,
    private val myName: String,
    private val host: String,
    private val port: Int,
    private val onStatus: (String) -> Unit,
    private val onChat: (ChatMsg) -> Unit,
    private val onHistory: (List<ChatMsg>) -> Unit,
    private val onJoined: (String) -> Unit,
    private val onEnded: (String) -> Unit,
    private val onVideoState: (Boolean) -> Unit,
    private val onFrame: (Bitmap) -> Unit,
    private val onFile: (name: String, path: String, media: Boolean) -> Unit,
    private val onMediaList: (List<Pair<String, Long>>) -> Unit,
    private val onMediaError: (String) -> Unit,
    private val onMetro: (on: Boolean, bpm: Int, beats: Int, hello: Boolean) -> Unit,
    private val saveFile: (name: String, data: ByteArray, media: Boolean) -> String,
    private val mediaTracks: () -> List<Pair<String, Long>>,
    private val mediaBytes: (String) -> ByteArray?,
) {
    private var socket: Socket? = null
    private var input: DataInputStream? = null
    private var out: OutputStream? = null
    private val outLock = Any()
    private val sentIds = java.util.Collections.synchronizedSet(HashSet<String>())

    @Volatile private var running = false

    @Volatile var audio: AudioEngine? = null

    fun connect() {
        if (running) return
        running = true
        Thread({
            try {
                val s = Socket()
                s.tcpNoDelay = true
                s.connect(InetSocketAddress(host, port), 6000)
                socket = s
                input = DataInputStream(BufferedInputStream(s.getInputStream()))
                out = BufferedOutputStream(s.getOutputStream())
                post { onStatus("Связь установлена, ждём преподавателя") }
                sendJson(mapOf("type" to "hello", "name" to myName))
                readLoop()
                if (running) post { onEnded("lost") }
            } catch (_: Exception) {
                if (running) post { onEnded("lost") }
            } finally {
                close()
            }
        }, "net-client").apply {
            isDaemon = true
            start()
        }
    }

    fun close() {
        running = false
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
        out = null
        input = null
        stopAudio()
    }

    fun sendChat(text: String): ChatMsg? {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return null
        val id = Proto.newId()
        val msg = ChatMsg(id, myName, trimmed, System.currentTimeMillis(), true)
        sentIds.add(id)
        post { onChat(msg) }
        sendJson(mapOf("type" to "chat", "id" to id, "from" to myName, "text" to trimmed))
        return msg
    }

    fun sendFile(name: String, data: ByteArray) {
        val o = out
        if (o == null) {
            post { onStatus("Нет связи — файл не отправлен") }
            return
        }
        try {
            Proto.send(o, outLock,
                mapOf("type" to "file_start", "name" to name, "size" to data.size, "media" to false),
                data)
        } catch (_: Exception) {
            post { onStatus("Файл не отправлен") }
        }
    }

    fun requestMediaList() {
        sendJson(mapOf("type" to "media_list_req"))
    }

    fun requestMedia(name: String) {
        sendJson(mapOf("type" to "media_get_req", "name" to name))
    }

    private fun sendMediaList() {
        val arr = JSONArray()
        for (t in mediaTracks()) {
            arr.put(JSONObject().put("name", t.first).put("size", t.second))
        }
        sendJson(mapOf("type" to "media_list", "tracks" to arr))
    }

    private fun sendTrack(name: String) {
        val data = if (name.isEmpty()) null else mediaBytes(name)
        val o = out
        if (data == null || o == null) {
            sendJson(mapOf("type" to "media_error", "text" to "Трек не найден"))
            return
        }
        try {
            Proto.send(o, outLock,
                mapOf("type" to "file_start", "name" to name,
                    "size" to data.size, "media" to true),
                data)
        } catch (_: Exception) {
        }
    }

    fun sendVideoState(on: Boolean) {
        sendJson(mapOf("type" to "video", "on" to on))
    }

    fun sendMetro(on: Boolean, bpm: Int, beats: Int, hello: Boolean = false) {
        sendJson(mapOf(
            "type" to "metro",
            "on" to on,
            "bpm" to bpm,
            "beats" to beats,
            "hello" to hello))
    }

    private fun sendJson(obj: Map<String, Any?>) {
        val o = out ?: return
        try {
            Proto.send(o, outLock, obj)
        } catch (_: Exception) {
        }
    }

    private fun post(body: () -> Unit) {
        handler.post(body)
    }

    private fun startAudio() {
        if (audio != null) return
        val engine = AudioEngine(host,
            { bmp -> post { onFrame(bmp) } },
            { text -> post { onStatus(text) } })
        engine.start()
        audio = engine
    }

    private fun stopAudio() {
        val engine = audio ?: return
        audio = null
        engine.stop()
    }

    private fun readLoop() {
        while (running) {
            val obj = try {
                Proto.recv(input ?: return)
            } catch (_: Exception) {
                return
            } ?: return
            when (obj.optString("type")) {
                "rejected" -> {
                    post { onEnded("rejected") }
                    return
                }
                "waiting" -> post { onStatus("Преподаватель занят — ждём") }
                "joined" -> {
                    val teacher = obj.optString("teacher", "Преподаватель")
                    post { onJoined(teacher) }
                    startAudio()
                }
                "ready" -> post { onStatus("Преподаватель освободился, можно говорить") }
                "ended" -> {
                    val reason = obj.optString("reason", "closed")
                    post { onEnded(reason) }
                    return
                }
                "chat" -> {
                    val id = obj.optString("id")
                    if (id.isNotEmpty() && sentIds.contains(id)) continue
                    val from = obj.optString("from", "")
                    if (from == myName && sentIds.isNotEmpty()) continue
                    val text = obj.optString("text", "")
                    if (text.isEmpty()) continue
                    val time = Proto.parseIso(obj.optString("time", "")).let {
                        if (it > 0) it else System.currentTimeMillis()
                    }
                    post { onChat(ChatMsg(id, from, text, time, false)) }
                }
                "chat_history" -> {
                    val list = ArrayList<ChatMsg>()
                    val arr = obj.optJSONArray("messages")
                    if (arr != null) {
                        for (i in 0 until arr.length()) {
                            val m = arr.optJSONObject(i) ?: continue
                            val text = m.optString("text", "")
                            if (text.isEmpty()) continue
                            val from = m.optString("from", "")
                            val time = Proto.parseIso(m.optString("time", "")).let {
                                if (it > 0) it else System.currentTimeMillis()
                            }
                            list.add(ChatMsg(
                                m.optString("id", Proto.newId()),
                                from, text, time, from == myName))
                        }
                    }
                    post { onHistory(list) }
                }
                "file_start" -> {
                    val name = obj.optString("name", "файл")
                    val size = obj.optInt("size", 0)
                    val media = obj.optBoolean("media", false)
                    if (size <= 0 || size > 200 * 1024 * 1024) continue
                    val data = Proto.readExact(input ?: return, size) ?: return
                    val path = saveFile(name, data, media)
                    post { onFile(name, path, media) }
                }
                "media_list" -> {
                    if (!obj.has("tracks")) {
                        // Просьба прислать список — отвечаем своими треками.
                        sendMediaList()
                    } else {
                        val tracks = ArrayList<Pair<String, Long>>()
                        val arr = obj.optJSONArray("tracks")
                        if (arr != null) {
                            for (i in 0 until arr.length()) {
                                val t = arr.optJSONObject(i) ?: continue
                                val name = t.optString("name", "")
                                if (name.isNotEmpty()) tracks.add(Pair(name, t.optLong("size", 0L)))
                            }
                        }
                        post { onMediaList(tracks) }
                    }
                }
                "media_list_req" -> sendMediaList()
                "media_get_req" -> sendTrack(obj.optString("name", ""))
                "media_get" -> sendTrack(obj.optString("name", ""))
                "media_error" -> {
                    val text = obj.optString("text", "Трек недоступен")
                    post { onMediaError(text) }
                }
                "video" -> {
                    val on = obj.optBoolean("on", false)
                    post { onVideoState(on) }
                }
                "metro" -> {
                    val on = obj.optBoolean("on", false)
                    val bpm = obj.optInt("bpm", 90)
                    val beats = obj.optInt("beats", 4)
                    val hello = obj.optBoolean("hello", false)
                    post { onMetro(on, bpm, beats, hello) }
                }
            }
        }
    }
}
