package tech.magiyastraha.majorlad

import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Запись хода программы в файл. Нужна для разбора неполадок со звуком:
 * владелец включает запись в настройках, повторяет занятие, останавливает
 * запись и отправляет файл в Telegram.
 *
 * Файл лежит во внутренней папке программы: посторонние его не видят,
 * наружу он уходит только через «Поделиться». Секретов в логе нет —
 * только состояния, счётчики и сообщения, которые и так видны на экране.
 */
object RunLog {
    private const val MAX_BYTES = 2L * 1024 * 1024
    private const val KEEP_FILES = 5
    private const val PREFIX = "majorlad-"

    private val lock = Any()
    private val timeFormat = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)
    private val nameFormat = SimpleDateFormat("yyyy-MM-dd_HH-mm-ss", Locale.US)

    private var out: BufferedWriter? = null
    private var file: File? = null

    @Volatile
    var isRunning = false
        private set

    /** Файл, который пишется прямо сейчас, или null. */
    fun current(): File? = file

    /** Начать запись. Возвращает файл или null, если открыть не удалось. */
    fun start(dir: File, header: String): File? = synchronized(lock) {
        if (isRunning) return file
        if (!dir.exists() && !dir.mkdirs()) return null
        cleanup(dir)
        val f = File(dir, PREFIX + nameFormat.format(Date()) + ".log")
        val w = try {
            BufferedWriter(FileWriter(f, true))
        } catch (_: Exception) {
            return null
        }
        out = w
        file = f
        isRunning = true
        writeLine("start", header)
        f
    }

    fun stop() {
        synchronized(lock) {
            if (!isRunning) return
            writeLine("stop", "запись лога остановлена")
            closeLocked()
        }
    }

    /** Дописать накопленное на диск, не закрывая файл — перед отправкой. */
    fun flush() {
        synchronized(lock) {
            try {
                out?.flush()
            } catch (_: Exception) {
            }
        }
    }

    fun write(tag: String, text: String) {
        synchronized(lock) {
            if (!isRunning) return
            writeLine(tag, text)
            val f = file ?: return
            if (f.length() > MAX_BYTES) {
                writeLine("log", "файл вырос больше двух мегабайт — запись остановлена")
                closeLocked()
            }
        }
    }

    /** Самый свежий лог в папке — на случай, если запись уже остановлена. */
    fun lastFile(dir: File): File? =
        dir.listFiles { f -> f.isFile && f.name.startsWith(PREFIX) }
            ?.maxByOrNull { it.lastModified() }

    private fun writeLine(tag: String, text: String) {
        val w = out ?: return
        try {
            w.write(timeFormat.format(Date()))
            w.write(" [")
            w.write(tag)
            w.write("] ")
            w.write(text.replace('\n', ' ').replace('\r', ' '))
            w.write("\n")
            w.flush()
        } catch (_: Exception) {
            closeLocked()
        }
    }

    private fun closeLocked() {
        try {
            out?.flush()
        } catch (_: Exception) {
        }
        try {
            out?.close()
        } catch (_: Exception) {
        }
        out = null
        file = null
        isRunning = false
    }

    /** Держим несколько последних логов, старые стираем: память телефона не бесконечна. */
    private fun cleanup(dir: File) {
        val files = dir.listFiles { f -> f.isFile && f.name.startsWith(PREFIX) } ?: return
        files.sortedByDescending { it.lastModified() }
            .drop(KEEP_FILES - 1)
            .forEach {
                try {
                    it.delete()
                } catch (_: Exception) {
                }
            }
    }
}
