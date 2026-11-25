package tech.magiyastraha.majorlad

import android.annotation.SuppressLint
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException

/**
 * Звук и видео урока: один UDP-сокет, как у программы на компьютере.
 *
 * Своё умение объявляем нулём флагов — без стерео и без Opus. Тогда
 * преподаватель шлёт звук несжатым PCM, а нам не нужен кодировщик Opus
 * (на телефоне его нет). Видео идёт теми же пакетами GCV1 с JPEG внутри.
 */
@SuppressLint("MissingPermission")
class AudioEngine(
    private val host: String,
    private val onFrame: (Bitmap) -> Unit,
    private val onLog: (String) -> Unit,
) {
    companion object {
        private const val SAMPLE_RATE = 48000
        private const val BLOCK = 480
        private const val UDP_PORT = 45801
    }

    @Volatile var micOn = true
    @Volatile private var playVolume = 1.0f
    @Volatile private var micGain = 1.0f
    @Volatile private var running = false

    private var socket: DatagramSocket? = null
    private var record: AudioRecord? = null
    private var track: AudioTrack? = null
    private var seq = 0
    private var loggedAudio = false

    // Сборка видеокадров: номер кадра -> куски.
    private val parts = HashMap<Long, HashMap<Int, ByteArray>>()
    private var lastFrame = -1L

    fun setPlayVolume(value: Float) {
        playVolume = value.coerceIn(0f, 1f)
        try {
            track?.setVolume(playVolume)
        } catch (_: Exception) {
        }
    }

    fun setMicGain(value: Float) {
        micGain = value.coerceIn(0f, 4f)
    }

    fun start() {
        if (running) return
        running = true
        try {
            val s = DatagramSocket()
            s.soTimeout = 500
            socket = s
        } catch (e: Exception) {
            onLog("Звук: не удалось открыть сеть — ${e.message}")
            running = false
            return
        }
        Thread({ helloLoop() }, "majorlad-hello").start()
        Thread({ recvLoop() }, "majorlad-recv").start()
        startMic()
    }

    fun stop() {
        running = false
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
        val rec = record
        record = null
        if (rec != null) {
            try {
                rec.stop()
            } catch (_: Exception) {
            }
            rec.release()
        }
        val t = track
        track = null
        if (t != null) {
            try {
                t.stop()
            } catch (_: Exception) {
            }
            t.release()
        }
    }

    // -- связь ---------------------------------------------------------------

    /** Раз в секунду говорим «я здесь» и повторяем свои возможности. */
    private fun helloLoop() {
        val s = socket ?: return
        val address = try {
            InetAddress.getByName(host)
        } catch (e: Exception) {
            onLog("Адрес звука не найден: $host")
            return
        }
        val hello = "GCH1".toByteArray(Charsets.US_ASCII)
        // GCA1 — несжатый моно PCM, стерео и Opus не поддерживаем.
        val caps = byteArrayOf('G'.code.toByte(), 'C'.code.toByte(), 'C'.code.toByte(),
            '2'.code.toByte(), 0)
        while (running) {
            try {
                s.send(DatagramPacket(hello, hello.size, address, UDP_PORT))
                s.send(DatagramPacket(caps, caps.size, address, UDP_PORT))
            } catch (_: Exception) {
                if (!running) break
            }
            try {
                Thread.sleep(1000)
            } catch (_: InterruptedException) {
                break
            }
        }
    }

    private fun recvLoop() {
        val s = socket ?: return
        val buf = ByteArray(65535)
        while (running) {
            val packet = DatagramPacket(buf, buf.size)
            try {
                s.receive(packet)
            } catch (_: SocketTimeoutException) {
                continue
            } catch (_: Exception) {
                break
            }
            val len = packet.length
            if (len < 5) continue
            when (String(buf, 0, 4, Charsets.US_ASCII)) {
                "GCA1" -> playPcm(buf, len, stereo = false)
                "GCA2" -> playPcm(buf, len, stereo = true)
                "GCV1" -> feedVideo(buf, len)
            }
        }
    }

    // -- звук ----------------------------------------------------------------

    private fun startMic() {
        try {
            val minBuf = AudioRecord.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT)
            val rec = AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, BLOCK * 2 * 8))
            if (rec.state != AudioRecord.STATE_INITIALIZED) {
                rec.release()
                onLog("Микрофон недоступен — проверьте разрешение.")
                return
            }
            rec.startRecording()
            record = rec
            Thread({ micLoop(rec) }, "majorlad-mic").start()
        } catch (e: Exception) {
            onLog("Микрофон: ${e.message}")
        }
    }

    private fun micLoop(rec: AudioRecord) {
        val s = socket ?: return
        val address = try {
            InetAddress.getByName(host)
        } catch (_: Exception) {
            return
        }
        val buffer = ShortArray(BLOCK)
        while (running) {
            val n = try {
                rec.read(buffer, 0, BLOCK)
            } catch (_: Exception) {
                break
            }
            if (n <= 0) continue
            if (!micOn) continue
            val gain = micGain
            val payload = ByteArray(n * 2)
            for (i in 0 until n) {
                var v = (buffer[i] * gain).toInt()
                if (v > 32767) v = 32767
                if (v < -32768) v = -32768
                payload[i * 2] = (v and 0xff).toByte()
                payload[i * 2 + 1] = ((v shr 8) and 0xff).toByte()
            }
            seq = (seq + 1) and 0x7fffffff
            val packet = ByteArray(8 + payload.size)
            packet[0] = 'G'.code.toByte()
            packet[1] = 'C'.code.toByte()
            packet[2] = 'A'.code.toByte()
            packet[3] = '1'.code.toByte()
            packet[4] = ((seq shr 24) and 0xff).toByte()
            packet[5] = ((seq shr 16) and 0xff).toByte()
            packet[6] = ((seq shr 8) and 0xff).toByte()
            packet[7] = (seq and 0xff).toByte()
            System.arraycopy(payload, 0, packet, 8, payload.size)
            try {
                s.send(DatagramPacket(packet, packet.size, address, UDP_PORT))
            } catch (_: Exception) {
                if (!running) break
            }
        }
    }

    private fun ensureTrack(): AudioTrack? {
        val existing = track
        if (existing != null) return existing
        return try {
            val minBuf = AudioTrack.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT)
            val t = AudioTrack(
                AudioManager.STREAM_MUSIC, SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, BLOCK * 2 * 8), AudioTrack.MODE_STREAM)
            t.setVolume(playVolume)
            t.play()
            track = t
            t
        } catch (e: Exception) {
            onLog("Динамик: ${e.message}")
            null
        }
    }

    private fun playPcm(data: ByteArray, len: Int, stereo: Boolean) {
        val t = ensureTrack() ?: return
        val start = 8
        if (len <= start) return
        if (!loggedAudio) {
            loggedAudio = true
            onLog("Звук урока пошёл.")
        }
        val bytes = if (stereo) downmix(data, start, len - start)
        else data.copyOfRange(start, len)
        if (bytes.isEmpty()) return
        try {
            t.write(bytes, 0, bytes.size)
        } catch (_: Exception) {
        }
    }

    /** Преподаватель может слать стерео — сводим в моно для телефона. */
    private fun downmix(data: ByteArray, offset: Int, length: Int): ByteArray {
        val frames = length / 4
        if (frames <= 0) return ByteArray(0)
        val out = ByteArray(frames * 2)
        for (i in 0 until frames) {
            val p = offset + i * 4
            val left = ((data[p].toInt() and 0xff) or (data[p + 1].toInt() shl 8)).toShort().toInt()
            val right = ((data[p + 2].toInt() and 0xff) or (data[p + 3].toInt() shl 8)).toShort().toInt()
            val v = (left + right) / 2
            out[i * 2] = (v and 0xff).toByte()
            out[i * 2 + 1] = ((v shr 8) and 0xff).toByte()
        }
        return out
    }

    // -- видео ---------------------------------------------------------------

    private fun feedVideo(data: ByteArray, len: Int) {
        if (len < 13) return
        val frameId = readUInt(data, 4)
        val index = readUShort(data, 8)
        val total = readUShort(data, 10)
        if (total <= 0 || index >= total) return
        if (frameId != 0L && frameId < lastFrame) return
        val payload = ByteArray(len - 12)
        System.arraycopy(data, 12, payload, 0, payload.size)

        var entry = parts[frameId]
        if (entry == null) {
            entry = HashMap()
            parts[frameId] = entry
        }
        entry[index] = payload
        // Больше четырёх кадров в сборке не держим.
        if (parts.size > 4) {
            val oldest = parts.keys.minOrNull() ?: frameId
            if (oldest != frameId) parts.remove(oldest)
        }
        val current = parts[frameId] ?: return
        if (current.size < total) return
        val frame = java.io.ByteArrayOutputStream()
        for (i in 0 until total) {
            val chunk = current[i]
            if (chunk == null) return
            frame.write(chunk)
        }
        parts.remove(frameId)
        lastFrame = frameId
        val bytes = frame.toByteArray()
        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
        if (bitmap != null) onFrame(bitmap)
    }

    private fun readUInt(data: ByteArray, at: Int): Long {
        return ((data[at].toLong() and 0xff) shl 24) or
            ((data[at + 1].toLong() and 0xff) shl 16) or
            ((data[at + 2].toLong() and 0xff) shl 8) or
            (data[at + 3].toLong() and 0xff)
    }

    private fun readUShort(data: ByteArray, at: Int): Int {
        return ((data[at].toInt() and 0xff) shl 8) or
            (data[at + 1].toInt() and 0xff)
    }
}
