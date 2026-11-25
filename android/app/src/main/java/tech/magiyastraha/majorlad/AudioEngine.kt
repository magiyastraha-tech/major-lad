package tech.magiyastraha.majorlad

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import java.io.ByteArrayOutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.util.concurrent.LinkedBlockingQueue
import kotlin.math.PI
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * Звук и видео между телефоном и собеседником. Работает в двух ролях:
 * у ученика адрес преподавателя известен сразу, у преподавателя адрес
 * ученика узнаётся из первого пришедшего пакета.
 *
 * Голос идёт непрерывным потоком: когда микрофон выключен, уходят тихие
 * отсчёты, поэтому метроном и разговор не рвутся.
 */
class AudioEngine(
    private val serverHost: String?,
    private val onFrame: (Bitmap) -> Unit,
    private val onLog: (String) -> Unit,
) {
    companion object {
        const val SAMPLE_RATE = 48000
        const val BLOCK = 480
        private const val CLICK_MS = 30
        private const val VIDEO_CHUNK = 1000
    }

    private val isServer = serverHost == null

    @Volatile var micOn = true
    @Volatile var playVolume = 0.9f
    @Volatile var micGain = 1.0f
    @Volatile var metroOn = false
    @Volatile var metroBpm = 90
    @Volatile var metroBeats = 4
    /** Щелчки, которые слышу я сам. */
    @Volatile var metroVolume = 0.7f
    /** Щелчки, которые уходят собеседнику вместе с моим голосом. */
    @Volatile var metroVolumeOut = 0.7f
    /** Собеседник включил свой метроном — свой щёлкать не должен. */
    @Volatile var peerMetroOn = false
    @Volatile private var running = false

    private var socket: DatagramSocket? = null
    @Volatile private var peer: InetSocketAddress? = null
    private val pcmQueue = LinkedBlockingQueue<ShortArray>()

    @Volatile private var micVersion = 0
    @Volatile private var micDevice: AudioDeviceInfo? = null
    @Volatile private var outDevice: AudioDeviceInfo? = null
    private var track: AudioTrack? = null
    @Volatile private var playClock = 0L
    @Volatile private var micClock = 0L
    private var videoId = 0
    private val videoFrames = HashMap<Int, VideoAccum>()

    private class VideoAccum(val total: Int) {
        val parts = arrayOfNulls<ByteArray>(total)
        var received = 0
    }

    fun start() {
        if (running) return
        running = true
        try {
            val s = if (isServer) {
                DatagramSocket(null).apply {
                    reuseAddress = true
                    bind(InetSocketAddress(Proto.UDP_PORT))
                }
            } else {
                DatagramSocket()
            }
            s.soTimeout = 500
            socket = s
        } catch (e: Exception) {
            onLog("Нет доступа к звуку по сети: ${e.message}")
            running = false
            return
        }
        if (!isServer) {
            try {
                peer = InetSocketAddress(InetAddress.getByName(serverHost), Proto.UDP_PORT)
            } catch (_: Exception) {
            }
        }
        launch("gca-hello") { helloLoop() }
        launch("gca-recv") { recvLoop() }
        launch("gca-mic") { micLoop() }
        launch("gca-play") { playLoop() }
    }

    fun stop() {
        running = false
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
        peer = null
        pcmQueue.clear()
        releaseTrack()
    }

    fun hasPeer(): Boolean = peer != null

    fun setMicDevice(dev: AudioDeviceInfo?) {
        if (micDevice?.id == dev?.id) return
        micDevice = dev
        micVersion++
    }

    fun setOutDevice(dev: AudioDeviceInfo?) {
        outDevice = dev
        try {
            track?.setPreferredDevice(dev ?: return)
        } catch (_: Exception) {
        }
    }

    /** Кадр видео уходит собеседнику частями; телефонный Wi-Fi не любит больших пакетов. */
    fun sendVideoFrame(jpeg: ByteArray) {
        val p = peer ?: return
        val s = socket ?: return
        val total = (jpeg.size + VIDEO_CHUNK - 1) / VIDEO_CHUNK
        if (total <= 0 || total > 60000) return
        val id = ++videoId
        for (index in 0 until total) {
            val from = index * VIDEO_CHUNK
            val to = min(jpeg.size, from + VIDEO_CHUNK)
            val buf = java.nio.ByteBuffer.allocate(12 + (to - from))
            buf.put(0x47); buf.put(0x43); buf.put(0x56); buf.put(0x31) // GCV1
            buf.putInt(id)
            buf.putShort(index.toShort())
            buf.putShort(total.toShort())
            buf.put(jpeg, from, to - from)
            try {
                s.send(DatagramPacket(buf.array(), buf.capacity(), p))
            } catch (_: Exception) {
                return
            }
        }
    }

    private fun launch(name: String, body: () -> Unit) {
        Thread(body, name).apply {
            isDaemon = true
            start()
        }
    }

    private fun sleep(ms: Long) {
        try {
            Thread.sleep(ms)
        } catch (_: InterruptedException) {
        }
    }

    private fun helloLoop() {
        val hello = "GCH1".toByteArray() + byteArrayOf(0, 0, 0, 0)
        // Флаг возможностей: ноль — шлём несжатый звук, его понимает компьютер.
        val caps = "GCC2".toByteArray() + byteArrayOf(0)
        while (running) {
            val p = peer
            val s = socket
            if (p != null && s != null) {
                try {
                    s.send(DatagramPacket(hello, hello.size, p))
                    s.send(DatagramPacket(caps, caps.size, p))
                } catch (_: Exception) {
                }
            }
            sleep(1000)
        }
    }

    private fun recvLoop() {
        val buf = ByteArray(64 * 1024)
        while (running) {
            val s = socket ?: break
            val packet = DatagramPacket(buf, buf.size)
            try {
                s.receive(packet)
            } catch (_: java.net.SocketTimeoutException) {
                continue
            } catch (_: Exception) {
                if (!running) break
                continue
            }
            if (packet.length < 4) continue
            if (isServer) {
                val cur = peer
                val addr = packet.address
                val port = packet.port
                if (cur == null || cur.address != addr || cur.port != port) {
                    peer = if (addr == null) null else InetSocketAddress(addr, port)
                    addr?.hostAddress?.let { onLog("Звук от $it") }
                }
            }
            val magic = String(packet.data, 0, 4, Charsets.US_ASCII)
            when (magic) {
                "GCA1" -> deliverPcm(packet, false)
                "GCA2" -> deliverPcm(packet, true)
                "GCV1" -> feedVideo(packet)
                "GCA5", "GCA6" -> onLog("Собеседник шлёт сжатый звук — на телефоне пока не поддерживается")
            }
        }
    }

    private fun deliverPcm(packet: DatagramPacket, stereo: Boolean) {
        val end = packet.length
        if (end <= 8) return
        val bytes = packet.data
        val out = ShortArray(if (stereo) (end - 8) / 4 else (end - 8) / 2)
        var off = 8
        var o = 0
        if (stereo) {
            while (off + 3 < end && o < out.size) {
                val l = ((bytes[off + 1].toInt() shl 8) or (bytes[off].toInt() and 0xff))
                val r = ((bytes[off + 3].toInt() shl 8) or (bytes[off + 2].toInt() and 0xff))
                out[o] = ((l + r) / 2).toShort()
                off += 4
                o++
            }
        } else {
            while (off + 1 < end && o < out.size) {
                out[o] = ((bytes[off + 1].toInt() shl 8) or (bytes[off].toInt() and 0xff)).toShort()
                off += 2
                o++
            }
        }
        while (pcmQueue.size > 8) pcmQueue.poll()
        pcmQueue.add(out)
    }

    private fun ensureTrack(): AudioTrack? {
        val exist = track
        if (exist != null && exist.state == AudioTrack.STATE_INITIALIZED) return exist
        return try {
            val minBuf = AudioTrack.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val size = max(minBuf, BLOCK * 2 * 4)
            val created = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build())
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(SAMPLE_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build())
                .setBufferSizeInBytes(size)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
            created.setVolume(playVolume)
            outDevice?.let { created.setPreferredDevice(it) }
            created.play()
            track = created
            created
        } catch (e: Exception) {
            onLog("Нет вывода звука: ${e.message}")
            null
        }
    }

    private fun releaseTrack() {
        try {
            track?.stop()
        } catch (_: Exception) {
        }
        try {
            track?.release()
        } catch (_: Exception) {
        }
        track = null
    }

    private fun playLoop() {
        val out = ShortArray(BLOCK)
        var pending: ShortArray? = null
        var pendingPos = 0
        while (running) {
            val t = ensureTrack()
            if (t == null) {
                sleep(200)
                continue
            }
            try {
                t.setVolume(playVolume)
            } catch (_: Exception) {
            }
            var filled = 0
            while (filled < BLOCK) {
                var chunk = pending
                if (chunk == null || pendingPos >= chunk.size) {
                    chunk = pcmQueue.poll() ?: break
                    pending = chunk
                    pendingPos = 0
                }
                val n = min(BLOCK - filled, chunk.size - pendingPos)
                System.arraycopy(chunk, pendingPos, out, filled, n)
                filled += n
                pendingPos += n
            }
            if (filled < BLOCK) java.util.Arrays.fill(out, filled, BLOCK, 0.toShort())
            if (metroOn && !peerMetroOn && metroVolume > 0.01f) {
                mixClicks(out, playClock, BLOCK, metroVolume)
            }
            playClock += BLOCK
            try {
                t.write(out, 0, BLOCK)
            } catch (_: Exception) {
                releaseTrack()
                sleep(100)
            }
        }
    }

    /** Щелчок метронома: первый удар в такте выше, остальные ниже. */
    private fun mixClicks(buf: ShortArray, startSample: Long, count: Int, volume: Float) {
        val bpm = metroBpm.coerceIn(20, 300)
        val beats = metroBeats.coerceIn(1, 12)
        val period = SAMPLE_RATE * 60.0 / bpm
        val clickLen = SAMPLE_RATE * CLICK_MS / 1000
        for (i in 0 until count) {
            val pos = startSample + i
            val phase = pos.toDouble() % period
            if (phase >= clickLen) continue
            val beat = (pos / period).toInt() % beats
            val freq = if (beat == 0) 1568.0 else 1046.0
            val env = 1.0 - phase / clickLen
            val add = sin(2 * PI * freq * phase / SAMPLE_RATE) * env * 0.4 * volume
            val mixed = buf[i] + (add * 32767).roundToInt()
            buf[i] = mixed.coerceIn(-32768, 32767).toShort()
        }
    }

    private fun micLoop() {
        while (running) {
            runMicSession()
            sleep(150)
        }
    }

    private fun runMicSession() {
        val myVersion = micVersion
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val size = max(minBuf, BLOCK * 2 * 8)
        var rec: AudioRecord? = null
        try {
            rec = AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(SAMPLE_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                        .build())
                .setBufferSizeInBytes(size)
                .build()
            micDevice?.let { rec.setPreferredDevice(it) }
            if (rec.state != AudioRecord.STATE_INITIALIZED) {
                throw IllegalStateException("микрофон занят другой программой")
            }
            rec.startRecording()
        } catch (e: Exception) {
            onLog("Нет микрофона: ${e.message}")
            try {
                rec?.release()
            } catch (_: Exception) {
            }
            return
        }
        val local = ShortArray(BLOCK)
        val packet = ByteArray(8 + BLOCK * 2)
        "GCA1".toByteArray().copyInto(packet, 0)
        var seq = 0
        while (running && micVersion == myVersion) {
            val read = try {
                rec.read(local, 0, BLOCK)
            } catch (_: Exception) {
                -1
            }
            if (read <= 0) {
                sleep(10)
                continue
            }
            // Сначала усиление голоса, потом щелчки. Иначе при выключенном
            // микрофоне вместе с голосом пропадал бы и метроном для собеседника.
            val gain = if (micOn) micGain else 0f
            for (i in 0 until read) {
                val v = if (gain <= 0f) 0 else (local[i] * gain).coerceIn(-32768f, 32767f).toInt()
                local[i] = v.toShort()
            }
            if (metroOn && !peerMetroOn && metroVolumeOut > 0.01f) {
                mixClicks(local, micClock, read, metroVolumeOut)
            }
            var off = 8
            for (i in 0 until read) {
                val v = local[i].toInt()
                packet[off++] = (v and 0xff).toByte()
                packet[off++] = ((v shr 8) and 0xff).toByte()
            }
            packet[4] = ((seq shr 24) and 0xff).toByte()
            packet[5] = ((seq shr 16) and 0xff).toByte()
            packet[6] = ((seq shr 8) and 0xff).toByte()
            packet[7] = (seq and 0xff).toByte()
            seq++
            micClock += read
            val p = peer
            val s = socket
            if (p != null && s != null) {
                try {
                    s.send(DatagramPacket(packet, off, p))
                } catch (_: Exception) {
                }
            }
        }
        try {
            rec.stop()
        } catch (_: Exception) {
        }
        try {
            rec.release()
        } catch (_: Exception) {
        }
    }

    private fun feedVideo(packet: DatagramPacket) {
        if (packet.length < 13) return
        val bb = java.nio.ByteBuffer.wrap(packet.data, 0, 12).order(java.nio.ByteOrder.BIG_ENDIAN)
        bb.position(4)
        val id = bb.int
        val index = bb.short.toInt() and 0xffff
        val total = bb.short.toInt() and 0xffff
        if (total <= 0 || total > 4096 || index >= total) return
        val payload = ByteArray(packet.length - 12)
        System.arraycopy(packet.data, 12, payload, 0, payload.size)
        val jpeg: ByteArray? = synchronized(videoFrames) {
            var acc = videoFrames[id]
            if (acc == null || acc.total != total) {
                acc = VideoAccum(total)
                videoFrames[id] = acc
            }
            if (acc.parts[index] == null) {
                acc.parts[index] = payload
                acc.received++
            }
            if (acc.received < acc.total) {
                while (videoFrames.size > 6) {
                    val oldest = videoFrames.keys.minOrNull() ?: break
                    if (oldest == id) break
                    videoFrames.remove(oldest)
                }
                null
            } else {
                val all = ByteArrayOutputStream()
                for (part in acc.parts) all.write(part ?: ByteArray(0))
                videoFrames.clear()
                all.toByteArray()
            }
        }
        if (jpeg != null) {
            val bmp = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)
            if (bmp != null) onFrame(bmp)
        }
    }
}
