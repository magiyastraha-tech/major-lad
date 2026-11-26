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
    private val udpPort: Int,
    private val onFrame: (Bitmap) -> Unit,
    private val onLog: (String) -> Unit,
) {
    companion object {
        const val SAMPLE_RATE = 48000
        const val BLOCK = 480
        private const val CLICK_MS = 30
        private const val VIDEO_CHUNK = 1000
        /** Сколько пакетов копим перед воспроизведением, чтобы рывки сети не давали щелчков. */
        private const val PREBUFFER = 3
        /** Длина плавного входа после провала, в отсчётах. */
        private const val FADE_IN = 96
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
    // Если микрофон открылся, но молчит, пробуем другой источник: на части
    // телефонов «громкая связь» отдаёт тишину, а обычный микрофон работает.
    @Volatile private var micSourceShift = 0
    @Volatile private var micRotations = 0
    @Volatile private var outDevice: AudioDeviceInfo? = null
    private var track: AudioTrack? = null
    @Volatile private var playClock = 0L
    @Volatile private var micClock = 0L

    // Счётчики для лога: по ним видно, доходит ли звук в обе стороны,
    // открылся ли микрофон и насколько громкий сигнал он отдаёт.
    @Volatile private var rxPackets = 0
    @Volatile private var rxAudioPackets = 0
    @Volatile private var txAudioPackets = 0
    @Volatile private var underruns = 0
    @Volatile private var micPeak = 0
    @Volatile private var peerPeak = 0
    @Volatile private var micLabel = "нет"
    @Volatile private var micActive = false
    private var lastMicError = ""
    private var lastMicErrorAt = 0L
    private var lastCompressedLog = 0L
    private var lastOutErrorAt = 0L
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
                    bind(InetSocketAddress(udpPort))
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
                peer = InetSocketAddress(InetAddress.getByName(serverHost), udpPort)
            } catch (_: Exception) {
            }
        }
        RunLog.write(
            "audio",
            (if (isServer) "режим преподавателя" else "режим ученика") +
                ", порт $udpPort, собеседник: " + (peer?.let { "${it.address.hostAddress}:${it.port}" } ?: "узнаём из пакета")
        )
        // Новый сеанс начинает с обычного источника и полным запасом
        // переключений: прошлое занятие ничего не должно тянуть за собой.
        micSourceShift = 0
        micRotations = 0
        launch("gca-hello") { helloLoop() }
        launch("gca-recv") { recvLoop() }
        launch("gca-mic") { micLoop() }
        launch("gca-play") { playLoop() }
        launch("gca-stats") { statsLoop() }
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
        RunLog.write("audio", "звук остановлен")
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
            rxPackets++
            if (isServer) {
                val cur = peer
                val addr = packet.address
                val port = packet.port
                if (cur == null || cur.address != addr || cur.port != port) {
                    peer = if (addr == null) null else InetSocketAddress(addr, port)
                    RunLog.write("audio", "собеседник: ${addr?.hostAddress}:$port")
                    addr?.hostAddress?.let { onLog("Звук от $it") }
                }
            }
            val magic = String(packet.data, 0, 4, Charsets.US_ASCII)
            when (magic) {
                "GCA1" -> deliverPcm(packet, false)
                "GCA2" -> deliverPcm(packet, true)
                "GCV1" -> feedVideo(packet)
                "GCA5", "GCA6" -> {
                    // Не засоряем лог: собеседник шлёт такой пакет десять раз в секунду.
                    val now = System.currentTimeMillis()
                    if (now - lastCompressedLog > 5000) {
                        lastCompressedLog = now
                        RunLog.write("audio", "пришёл сжатый звук $magic — на телефоне пока не поддерживается")
                        onLog("Собеседник шлёт сжатый звук — на телефоне пока не поддерживается")
                    }
                }
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
        if (out.isEmpty()) return
        rxAudioPackets++
        var peak = 0
        for (v in out) {
            val a = if (v < 0) -v.toInt() else v.toInt()
            if (a > peak) peak = a
        }
        if (peak > peerPeak) peerPeak = peak
        while (pcmQueue.size > 8) pcmQueue.poll()
        pcmQueue.add(out)
    }

    private fun ensureTrack(): AudioTrack? {
        val exist = track
        if (exist != null && exist.state == AudioTrack.STATE_INITIALIZED) return exist
        return try {
            val minBuf = AudioTrack.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            // Запас побольше: на маленьком буфере телефон не успевает
            // проигрывать сглаженные провалы и звук трещит.
            val size = max(minBuf, BLOCK * 2 * 8)
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
            RunLog.write("audio", "вывод звука открыт, буфер $size байт")
            created
        } catch (e: Exception) {
            val now = System.currentTimeMillis()
            if (now - lastOutErrorAt > 4000) {
                lastOutErrorAt = now
                RunLog.write("audio", "вывод звука не открылся: ${e.message}")
                onLog("Нет вывода звука: ${e.message}")
            }
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

    /**
     * Воспроизведение. Копим небольшой запас, а на провале не обрываем
     * звук в ноль (это слышно как щелчок), а плавно гасим и так же плавно
     * возвращаем. Свой метроном в это время продолжает тикать.
     */
    private fun playLoop() {
        val out = ShortArray(BLOCK)
        var pending: ShortArray? = null
        var pendingPos = 0
        var primed = false
        var fadeIn = false
        var lastValue = 0
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
            // Ждём запас только когда слушаем собеседника. Если тикает свой
            // метроном, ждать нечего — иначе такт сорвался бы.
            val needPrebuffer = !(metroOn && !peerMetroOn)
            if (!primed && needPrebuffer && pcmQueue.size < PREBUFFER) {
                sleep(5)
                continue
            }
            primed = true
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
            if (filled < BLOCK) {
                underruns++
                var v = lastValue
                for (i in filled until BLOCK) {
                    v = (v * 15) / 16
                    out[i] = v.toShort()
                }
                lastValue = v
                fadeIn = true
                primed = false
            } else {
                if (fadeIn) {
                    val n = min(BLOCK, FADE_IN)
                    for (i in 0 until n) {
                        out[i] = ((out[i].toInt() * i) / n).toShort()
                    }
                    fadeIn = false
                }
                lastValue = out[BLOCK - 1].toInt()
            }
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
            // Микрофон не открылся — не дёргаем телефон двадцать раз в
            // секунду, ждём подольше и пробуем снова.
            sleep(if (micLabel == "нет") 1000 else 150)
        }
        micActive = false
    }

    /**
     * Источники перебираем по очереди: на части телефонов «громкая связь»
     * не открывается или отдаёт одну тишину, а обычный микрофон работает.
     */
    private val micSources = listOf(
        MediaRecorder.AudioSource.VOICE_COMMUNICATION to "громкая связь",
        MediaRecorder.AudioSource.MIC to "микрофон",
        MediaRecorder.AudioSource.DEFAULT to "по умолчанию",
    )

    private fun openMic(size: Int): AudioRecord? {
        var lastError = ""
        for (n in micSources.indices) {
            val (source, label) = micSources[(n + micSourceShift) % micSources.size]
            if (!running) return null
            var rec: AudioRecord? = null
            try {
                rec = AudioRecord.Builder()
                    .setAudioSource(source)
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(SAMPLE_RATE)
                            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                            .build())
                    .setBufferSizeInBytes(size)
                    .build()
                if (rec.state != AudioRecord.STATE_INITIALIZED) {
                    throw IllegalStateException("микрофон занят другой программой")
                }
                val dev = micDevice
                if (dev != null) {
                    val ok = try {
                        rec.setPreferredDevice(dev)
                    } catch (_: Exception) {
                        false
                    }
                    if (!ok) RunLog.write("audio", "выбранный микрофон недоступен — беру обычный")
                }
                rec.startRecording()
                if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                    throw IllegalStateException("запись не началась")
                }
                micLabel = label
                micActive = true
                RunLog.write("audio", "микрофон открыт: $label, буфер $size байт")
                if (lastMicError.isNotEmpty()) {
                    lastMicError = ""
                    onLog("Микрофон заработал")
                }
                return rec
            } catch (e: Exception) {
                lastError = e.message ?: e.javaClass.simpleName
                try {
                    rec?.release()
                } catch (_: Exception) {
                }
            }
        }
        micActive = false
        micLabel = "нет"
        val now = System.currentTimeMillis()
        // Одно и то же сообщение не чаще раза в 4 секунды: иначе экран
        // и лог забиваются повторами.
        if (lastError != lastMicError || now - lastMicErrorAt > 4000) {
            lastMicError = lastError
            lastMicErrorAt = now
            RunLog.write("audio", "микрофон не открылся: $lastError")
            onLog("Нет микрофона: $lastError")
        }
        return null
    }

    private fun runMicSession() {
        val myVersion = micVersion
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val size = max(if (minBuf > 0) minBuf else BLOCK * 2 * 8, BLOCK * 2 * 8)
        val rec = openMic(size) ?: return
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
            var peak = 0
            for (i in 0 until read) {
                val raw = local[i].toInt()
                val a = if (raw < 0) -raw else raw
                if (a > peak) peak = a
            }
            if (peak > micPeak) micPeak = peak
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
                    txAudioPackets++
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
        micActive = false
        RunLog.write("audio", "микрофон закрыт")
    }

    /**
     * Раз в две секунды пишем в лог, что происходит со звуком. По этим
     * строчкам видно, где обрыв: не открывается микрофон, микрофон отдаёт
     * тишину, пакеты не доходят или не доходит сам звук.
     */
    private fun statsLoop() {
        var lastRx = 0
        var lastRxAudio = 0
        var lastTx = 0
        var lastUnder = 0
        var silentRounds = 0
        var peerSilentRounds = 0
        var peerSilentWarned = false
        var noAudioRounds = 0
        var noAudioWarned = false
        while (running) {
            sleep(2000)
            if (!running) break
            val rx = rxPackets
            val rxa = rxAudioPackets
            val tx = txAudioPackets
            val un = underruns
            val myPeak = micPeak
            micPeak = 0
            val theirPeak = peerPeak
            peerPeak = 0
            val dRx = rx - lastRx
            val dRxAudio = rxa - lastRxAudio
            val dTx = tx - lastTx
            RunLog.write(
                "audio",
                "за 2 с: принято $dRx пакетов (со звуком $dRxAudio), отправлено звука $dTx, " +
                    "провалы ${un - lastUnder}, в очереди ${pcmQueue.size}, " +
                    "мой микрофон ${if (micActive) micLabel else "не открыт"} " +
                    "на ${myPeak * 100 / 32768}%, звук собеседника на ${theirPeak * 100 / 32768}%, " +
                    "собеседник ${if (peer != null) "есть" else "нет"}"
            )
            lastRx = rx
            lastRxAudio = rxa
            lastTx = tx
            lastUnder = un

            // Микрофон открыт и включён, а сигнала нет — так микрофон не работает.
            if (micActive && micOn && myPeak == 0) {
                silentRounds++
                if (silentRounds == 3) {
                    RunLog.write("audio", "микрофон открыт, но отдаёт только тишину")
                }
                // Двенадцать секунд полной тишины — это уже не пауза в
                // разговоре, а неработающий источник. Пробуем следующий:
                // на части телефонов «громкая связь» всегда отдаёт нули.
                // Выбранный в настройках микрофон остаётся, меняется только
                // способ его открыть.
                if (silentRounds >= 6 && micRotations < micSources.size - 1) {
                    silentRounds = 0
                    micRotations++
                    micSourceShift = (micSourceShift + 1) % micSources.size
                    val next = micSources[micSourceShift].second
                    RunLog.write(
                        "audio",
                        "микрофон молчит 12 секунд — переключаюсь на источник «$next» ($micRotations)"
                    )
                    onLog("Микрофон молчит — переключаюсь на другой источник")
                    micVersion++
                }
            } else {
                silentRounds = 0
                // Сигнал слышен — значит текущий источник рабочий, и
                // запас попыток на будущее снова полный.
                if (myPeak > 0) micRotations = 0
            }
            // Звук идёт, но в нём одни нули — у собеседника выключен микрофон.
            if (dRxAudio > 0 && theirPeak == 0) {
                peerSilentRounds++
                if (peerSilentRounds >= 2 && !peerSilentWarned) {
                    peerSilentWarned = true
                    onLog("У собеседника выключен микрофон — попросите включить звук")
                }
            } else {
                peerSilentRounds = 0
                if (theirPeak > 0) peerSilentWarned = false
            }
            // Собеседник есть, а звука от него нет совсем — у него не
            // открылся микрофон.
            if (peer != null && dRxAudio == 0) {
                noAudioRounds++
                if (noAudioRounds >= 3 && !noAudioWarned) {
                    noAudioWarned = true
                    RunLog.write("audio", "собеседник подключён, но звук от него не идёт")
                    onLog("Звук от собеседника не идёт — проверьте, что у него работает микрофон")
                }
            } else {
                noAudioRounds = 0
                if (dRxAudio > 0) noAudioWarned = false
            }
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
