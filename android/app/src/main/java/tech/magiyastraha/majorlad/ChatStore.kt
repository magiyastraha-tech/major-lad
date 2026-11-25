package tech.magiyastraha.majorlad

import android.content.Context
import org.json.JSONArray
import java.io.File

/**
 * Переписка на телефоне. Хранится одним файлом в личной папке приложения:
 * переживает закрытие программы, но не уходит никуда наружу.
 */
class ChatStore(private val ctx: Context) {

    private val file = File(ctx.filesDir, "chat.json")
    private val messages = ArrayList<ChatMsg>()

    val all: List<ChatMsg> get() = messages

    init {
        load()
    }

    fun add(msg: ChatMsg) {
        messages.add(msg)
        while (messages.size > 2000) messages.removeAt(0)
        save()
    }

    fun replaceAll(list: List<ChatMsg>) {
        messages.clear()
        messages.addAll(list)
        save()
    }

    /** Сообщения, начиная с первого в этот день, для заголовка с датой. */
    fun page(before: String, limit: Int): Pair<List<ChatMsg>, Boolean> {
        var source: List<ChatMsg> = messages
        if (before.isNotEmpty()) {
            val index = messages.indexOfFirst { it.id == before }
            if (index <= 0) return Pair(emptyList(), false)
            source = messages.subList(0, index)
        }
        val from = maxOf(0, source.size - limit.coerceIn(1, 200))
        val chunk = ArrayList(source.subList(from, source.size))
        return Pair(chunk, from > 0)
    }

    private fun load() {
        try {
            if (!file.exists()) return
            val arr = JSONArray(file.readText(Charsets.UTF_8))
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                messages.add(ChatMsg.fromJson(o))
            }
        } catch (_: Exception) {
        }
    }

    private fun save() {
        try {
            val arr = JSONArray()
            for (m in messages) arr.put(m.toJson())
            val tmp = File(ctx.filesDir, "chat.json.tmp")
            tmp.writeText(arr.toString(), Charsets.UTF_8)
            tmp.renameTo(file)
        } catch (_: Exception) {
        }
    }
}
