package tech.magiyastraha.majorlad

import org.json.JSONObject
import java.io.InputStream
import java.io.OutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Одно сообщение переписки. Время — миллисекунды, как в Java. */
data class ChatMsg(
    val id: String,
    val from: String,
    val text: String,
    val time: Long,
    val mine: Boolean = false,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("from", from)
        .put("text", text)
        .put("time", time)
        .put("mine", mine)

    companion object {
        fun fromJson(o: JSONObject) = ChatMsg(
            o.optString("id", ""),
            o.optString("from", ""),
            o.optString("text", ""),
            o.optLong("time", 0L),
            o.optBoolean("mine", false))
    }
}

/**
 * Общие числа и кадрирование протокола «Мажорного лада».
 * Компьютерная программа говорит теми же сообщениями: четыре байта длины
 * и JSON в UTF-8.
 */
object Proto {
    /** Порт по умолчанию: и для связи, и для звука. Его можно сменить в настройках. */
    const val APP_PORT = 45801

    fun send(
        out: OutputStream,
        lock: Any,
        obj: Map<String, Any?>,
        payload: ByteArray? = null,
    ) {
        val data = JSONObject(obj).toString().toByteArray(Charsets.UTF_8)
        val head = byteArrayOf(
            ((data.size shr 24) and 0xff).toByte(),
            ((data.size shr 16) and 0xff).toByte(),
            ((data.size shr 8) and 0xff).toByte(),
            (data.size and 0xff).toByte())
        synchronized(lock) {
            out.write(head)
            out.write(data)
            if (payload != null) out.write(payload)
            out.flush()
        }
    }

    fun recv(input: InputStream): JSONObject? {
        val head = readExact(input, 4) ?: return null
        val size = ((head[0].toInt() and 0xff) shl 24) or
            ((head[1].toInt() and 0xff) shl 16) or
            ((head[2].toInt() and 0xff) shl 8) or
            (head[3].toInt() and 0xff)
        if (size < 0 || size > 64 * 1024 * 1024) {
            throw java.io.IOException("слишком большое сообщение")
        }
        val body = readExact(input, size) ?: return null
        return JSONObject(String(body, Charsets.UTF_8))
    }

    fun readExact(input: InputStream, n: Int): ByteArray? {
        val out = ByteArray(n)
        var offset = 0
        while (offset < n) {
            val read = try {
                input.read(out, offset, n - offset)
            } catch (_: Exception) {
                -1
            }
            if (read <= 0) return null
            offset += read
        }
        return out
    }

    /** Время из истории компьютерной программы: 2026-11-25T14:03:00. */
    fun parseIso(text: String): Long {
        if (text.isEmpty()) return 0L
        val patterns = arrayOf("yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd HH:mm:ss")
        for (pattern in patterns) {
            try {
                val date = SimpleDateFormat(pattern, Locale.US).parse(text)
                if (date != null) return date.time
            } catch (_: Exception) {
            }
        }
        return 0L
    }

    fun timeText(millis: Long): String =
        SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(millis))

    fun dateText(millis: Long): String =
        SimpleDateFormat("d MMMM yyyy", Locale.getDefault()).format(Date(millis))

    fun dayKey(millis: Long): String =
        SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date(millis))

    fun newId(): String =
        java.util.UUID.randomUUID().toString().replace("-", "").substring(0, 8)
}
