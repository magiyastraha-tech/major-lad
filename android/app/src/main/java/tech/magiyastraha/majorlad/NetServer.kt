package tech.magiyastraha.majorlad

import android.graphics.Bitmap
import android.os.Handler
import org.json.JSONArray
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket

/**
 * Преподаватель держит дверь открытой: ждёт ученика, разговаривает с ним,
 * отдаёт ему треки. Один ученик за раз — как на компьютере.
 */
class NetServer(
    private val handler: Handler,
    private val myName: String,
    private val port: Int,
    private val onStatus: (String) -> Unit,
    private val onChat: (ChatMsg) -> Unit,
    private val onPeerJoined: (String) -> Unit,
    private val onPeerLeft: (String) -> Unit,
    private val onVideoState: (Boolean) -> Unit,
    private val onFrame: (Bitmap) -> Unit,
    private val onFile: (name: String, path: String, media: Boolean) -> Unit,
    private val onMetro: (on: Boolean, bpm: Int, beats: Int, hello: Boolean) -> Unit,
    private val history: (before: String, limit: Int) -> Pair<List<ChatMsg>, Boolean>,
    private val mediaTracks: () -> List<Pair<String, Long>>,
    private val mediaBytes: (String) -> ByteArray?,
    private val onMediaList: (List<Pair<String, Long>>) -> Unit,
    private val onMediaError: (String) -> Unit,
    private val saveFile: (name: String, data: ByteArray, media: Boolean) -> String,
) {
    private var serverSocket: ServerSocket? = null
    @Volatile private var running = false

    private var peer: Socket? = null
    private var input: DataInputStream? = null
    private var out: OutputStream? = null
    private val outLock = Any()
    @Volatile private var peerName = ""

    @Volatile var audio: AudioEngine? = null

    fun start() {
        if (running) return
        running = true
        Thread({
            try {
                val s = ServerSocket()
                s.reuseAddress = true
                s.bind(InetSocketAddress(port))
                serverSocket = s
                post { onStatus("Кабинет открыт, ждём ученика") }
                while (running) {
                    val client = try {
                        s.accept()
                    } catch (_: Exception) {
                        break
                    }
                    handle(client)
                }
            } catch (e: Exception) {
                post { onStatus("Не удалось открыть кабинет: ${e.message}") }
            }
        }, "net-server").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        running = false
        try {
            serverSocket?.close()
        } catch (_: Exception) {
        }
        serverSocket = null
        hangUp("closed")
    }

    fun isConnected(): Boolean = peer != null

    fun peerName(): String = peerName

    fun sendChat(text: String): ChatMsg? {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return null
        val id = Proto.newId()
        val msg = ChatMsg(id, myName, trimmed, System.currentTimeMillis(), true)
        sendJson(mapOf("type" to "chat", "id" to id, "from" to myName, "text" to trimmed))
        return msg
    }

    fun sendFile(name: String, data: ByteArray) {
        if (peer == null) return
        val o = out ?: return
        try {
            Proto.send(o, outLock,
                mapOf("type" to "file_start", "name" to name, "size" to data.size, "media" to false),
                data)
        } catch (_: Exception) {
        }
    }

    /** Спросить у собеседника список его треков. */
    fun requestMediaList() {
        sendJson(mapOf("type" to "media_list_req"))
    }

    /** Попросить у собеседника один трек — он придёт файлом с пометкой media. */
    fun requestMedia(name: String) {
        sendJson(mapOf("type" to "media_get_req", "name" to name))
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

    private fun handle(client: Socket) {
        val busy = peer != null
        try {
            client.tcpNoDelay = true
            val inStream = DataInputStream(BufferedInputStream(client.getInputStream()))
            val outStream = BufferedOutputStream(client.getOutputStream())
            val hello = try {
                Proto.recv(inStream)
            } catch (_: Exception) {
                null
            }
            if (busy) {
                try {
                    Proto.send(outStream, Any(),
                        mapOf("type" to "rejected", "reason" to "busy", "text" to "Преподаватель занят"))
                } catch (_: Exception) {
                }
                try {
                    client.close()
                } catch (_: Exception) {
                }
                post { onStatus("Кто-то стучался, но вы уже разговариваете") }
                return
            }
            if (hello == null || hello.optString("type") != "hello") {
                try {
                    client.close()
                } catch (_: Exception) {
                }
                return
            }
            val name = hello.optString("name", "Ученик").ifEmpty { "Ученик" }
            peer = client
            input = inStream
            out = outStream
            peerName = name
            try {
                Proto.send(outStream, outLock,
                    mapOf("type" to "joined", "teacher" to myName))
                val (chunk, more) = history("", 50)
                sendHistory(chunk, more)
            } catch (_: Exception) {
                hangUp("lost")
                return
            }
            val engine = AudioEngine(null,
                { bmp -> post { onFrame(bmp) } },
                { text -> post { onStatus(text) } })
            engine.start()
            audio = engine
            post {
                onPeerJoined(name)
                onStatus("$name подключился")
            }
            readLoop(inStream)
        } catch (_: Exception) {
        } finally {
            if (peer === client) hangUp("lost")
        }
    }

    private fun readLoop(inStream: DataInputStream) {
        while (running && peer != null) {
            val obj = try {
                Proto.recv(inStream)
            } catch (_: Exception) {
                return
            } ?: return
            when (obj.optString("type")) {
                "chat" -> {
                    val text = obj.optString("text", "")
                    if (text.isEmpty()) continue
                    val from = obj.optString("from", peerName)
                    val msg = ChatMsg(obj.optString("id", Proto.newId()), from, text,
                        System.currentTimeMillis(), false)
                    post { onChat(msg) }
                }
                "chat_history_req" -> {
                    val before = obj.optString("before", "")
                    val limit = obj.optInt("limit", 50)
                    val (chunk, more) = history(before, limit)
                    sendHistory(chunk, more)
                }
                "file_start" -> {
                    val name = obj.optString("name", "файл")
                    val size = obj.optInt("size", 0)
                    val media = obj.optBoolean("media", false)
                    if (size <= 0 || size > 200 * 1024 * 1024) continue
                    val data = Proto.readExact(inStream, size) ?: return
                    val path = saveFile(name, data, media)
                    post { onFile(name, path, media) }
                }
                // media_list без ключа tracks — просьба прислать список;
                // с ключом — ответ собеседника. Отвечать на ответ нельзя,
                // иначе два телефона будут перекидываться списками по кругу.
                "media_list" -> {
                    if (obj.has("tracks")) {
                        val arr = obj.optJSONArray("tracks") ?: JSONArray()
                        val tracks = ArrayList<Pair<String, Long>>()
                        for (i in 0 until arr.length()) {
                            val t = arr.optJSONObject(i) ?: continue
                            tracks.add(t.optString("name") to t.optLong("size", 0L))
                        }
                        post { onMediaList(tracks) }
                    } else {
                        sendMediaList()
                    }
                }
                "media_list_req" -> sendMediaList()
                "media_get_req" -> {
                    val name = obj.optString("name", "")
                    sendTrack(name)
                }
                "media_get" -> {
                    // Старая форма запроса: прислать трек по имени.
                    sendTrack(obj.optString("name", ""))
                }
                "media_error" -> {
                    val text = obj.optString("text", "Трек не найден")
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

    private fun sendMediaList() {
        val arr = JSONArray()
        for (t in mediaTracks()) {
            arr.put(org.json.JSONObject()
                .put("name", t.first)
                .put("size", t.second))
        }
        sendJson(mapOf("type" to "media_list", "tracks" to arr))
    }

    private fun sendTrack(name: String) {
        val data = if (name.isEmpty()) null else mediaBytes(name)
        if (data == null) {
            sendJson(mapOf("type" to "media_error", "text" to "Трек не найден"))
            return
        }
        val o = out ?: return
        try {
            Proto.send(o, outLock,
                mapOf("type" to "file_start", "name" to name,
                    "size" to data.size, "media" to true),
                data)
        } catch (_: Exception) {
        }
    }

    private fun sendHistory(chunk: List<ChatMsg>, more: Boolean) {
        val arr = JSONArray()
        for (m in chunk) {
            arr.put(org.json.JSONObject()
                .put("id", m.id)
                .put("time", isoTime(m.time))
                .put("from", m.from)
                .put("text", m.text))
        }
        sendJson(mapOf("type" to "chat_history", "messages" to arr, "more" to more))
    }

    private fun isoTime(millis: Long): String {
        val fmt = java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", java.util.Locale.US)
        return fmt.format(java.util.Date(millis))
    }

    private fun hangUp(reason: String) {
        val had = peer != null
        val name = peerName
        try {
            peer?.close()
        } catch (_: Exception) {
        }
        peer = null
        input = null
        out = null
        audio?.stop()
        audio = null
        if (had) post {
            onPeerLeft(reason)
            onStatus(if (name.isEmpty()) "Ученик отключился" else "$name отключился")
        }
    }
}
