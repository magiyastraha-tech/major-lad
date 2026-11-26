package tech.magiyastraha.majorlad

import android.media.MediaCodec
import android.media.MediaCodecList
import android.media.MediaFormat
import java.nio.ByteOrder

/**
 * Opus через MediaCodec: 192 кбит/с вместо несжатого PCM.
 *
 * Несжатый звук — это 768 кбит/с в одну сторону. Opus ужимает его в четыре
 * раза без слышимой потери для голоса и гитары, и канал остаётся свободным
 * для видео. На Android Opus появился не сразу: декодер есть с Android 5,
 * кодировщик — только с Android 10. Поэтому обе возможности проверяются на
 * этом конкретном телефоне, а не по номеру версии системы: список кодеков
 * честнее. Нет кодировщика — звук идёт AAC-LC (`AacCodec`), он есть всюду.
 */
object OpusSupport {
    const val MIME = "audio/opus"
    const val BITRATE = 192000

    /** Телефон умеет раскодировать Opus — об этом сообщаем собеседнику. */
    val decoderAvailable: Boolean by lazy { probe(false) }

    @Volatile
    private var decoderBroken = false
    private var selfTested = false
    private var selfTestOk = false

    /**
     * Можно ли честно обещать собеседнику Opus прямо сейчас.
     * Железо есть, пробный пакет реально раскодировался, и декодер ещё
     * не срывался на живом звуке. Список кодеков сам по себе ничего не
     * доказывает: на части прошивок декодер открывается и сразу умирает.
     */
    fun decoderUsable(): Boolean {
        if (!decoderAvailable || decoderBroken) return false
        synchronized(this) {
            if (!selfTested) {
                selfTestOk = probeDecode()
                selfTested = true
                RunLog.write(
                    "audio",
                    if (selfTestOk) {
                        "Opus: пробный пакет раскодирован"
                    } else {
                        "Opus: пробный пакет не раскодировался — сжатый звук не предлагаем"
                    }
                )
            }
        }
        return selfTestOk
    }

    /** Заявка снимается до конца занятия: собеседник сам перейдёт на AAC. */
    fun markDecoderBroken() {
        decoderBroken = true
    }

    /** Тишина в Opus: кодируем нули и пробуем раскодировать обратно. */
    private fun probeDecode(): Boolean {
        // Свой кодировщик не завёлся — пробуем готовым кадром тишины.
        val packet = probePacket()
            ?: byteArrayOf(0xF8.toByte(), 0xFF.toByte(), 0xFE.toByte())
        return try {
            val format = MediaFormat.createAudioFormat(MIME, AudioEngine.SAMPLE_RATE, 1)
            val name = softwareDecoderName()
            val c = if (name != null) {
                MediaCodec.createByCodecName(name)
            } else {
                MediaCodec.createDecoderByType(MIME)
            }
            try {
                c.configure(format, null, null, 0)
                c.start()
                val info = MediaCodec.BufferInfo()
                var queued = false
                val deadline = System.currentTimeMillis() + 500
                while (System.currentTimeMillis() < deadline) {
                    if (!queued) {
                        val i = c.dequeueInputBuffer(5_000)
                        if (i >= 0) {
                            val buf = c.getInputBuffer(i) ?: return false
                            buf.clear()
                            buf.put(packet)
                            c.queueInputBuffer(i, 0, packet.size, 0, 0)
                            queued = true
                        }
                    } else {
                        val o = c.dequeueOutputBuffer(info, 5_000)
                        if (o >= 0) {
                            c.releaseOutputBuffer(o, false)
                            return true
                        }
                    }
                }
                false
            } finally {
                try {
                    c.stop()
                } catch (_: Exception) {
                }
                try {
                    c.release()
                } catch (_: Exception) {
                }
            }
        } catch (e: Exception) {
            RunLog.write("audio", "Opus: проба декодера не прошла: ${e.message}")
            false
        }
    }

    private fun probePacket(): ByteArray? {
        if (!encoderAvailable) return null
        var packet: ByteArray? = null
        val enc = OpusEncoder({ p -> if (packet == null) packet = p }, {}, quiet = true)
        if (!enc.start()) return null
        try {
            val silence = ShortArray(960)
            var tries = 0
            while (packet == null && tries < 50) {
                enc.encode(silence, silence.size)
                if (packet == null) Thread.sleep(5)
                tries++
            }
            return packet
        } catch (_: Exception) {
            return null
        } finally {
            enc.release()
        }
    }

    /**
     * Предпочитаем штатный софтверный декодер AOSP: вендорские декодеры
     * на части прошивок открываются и сразу умирают (Released state).
     */
    fun softwareDecoderName(): String? = try {
        val format = MediaFormat.createAudioFormat(MIME, AudioEngine.SAMPLE_RATE, 1)
        val list = MediaCodecList(MediaCodecList.REGULAR_CODECS)
        val infos = list.codecInfos
        val preferred = listOf("c2.android.opus.decoder", "OMX.google.opus.decoder")
        preferred.firstOrNull { name -> infos.any { !it.isEncoder && it.name == name } }
            ?: list.findDecoderForFormat(format)
    } catch (_: Exception) {
        null
    }

    /** Телефон умеет и закодировать; иначе отправляем AAC. */
    val encoderAvailable: Boolean by lazy { probe(true) }

    @Volatile
    private var encoderBroken = false

    /** Кодировщик Opus сорвался на живом звуке — больше не пробуем. */
    fun encoderUsable(): Boolean = encoderAvailable && !encoderBroken

    fun markEncoderBroken() {
        encoderBroken = true
    }

    private fun probe(encode: Boolean): Boolean = try {
        val format = MediaFormat.createAudioFormat(MIME, AudioEngine.SAMPLE_RATE, 1)
        val list = MediaCodecList(MediaCodecList.REGULAR_CODECS)
        val name = if (encode) {
            list.findEncoderForFormat(format)
        } else {
            list.findDecoderForFormat(format)
        }
        name != null
    } catch (_: Exception) {
        false
    }
}

/** Короткий блок PCM -> один пакет Opus. */
class OpusEncoder(
    private val onPacket: (ByteArray) -> Unit,
    private val onLog: (String) -> Unit,
    // Пробный кодировщик не должен засорять журнал: он живёт полсекунды.
    private val quiet: Boolean = false,
) {
    private var codec: MediaCodec? = null
    private val info = MediaCodec.BufferInfo()
    private var pts = 0L
    private var broken = false

    /** Кодировщик сломался: звук надо передать другому формату. */
    val isBroken: Boolean get() = broken

    fun start(): Boolean {
        if (codec != null) return true
        if (broken || !OpusSupport.encoderAvailable) return false
        return try {
            val format = MediaFormat.createAudioFormat(
                OpusSupport.MIME, AudioEngine.SAMPLE_RATE, 1
            ).apply {
                setInteger(MediaFormat.KEY_BIT_RATE, OpusSupport.BITRATE)
            }
            val c = MediaCodec.createEncoderByType(OpusSupport.MIME)
            c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            c.start()
            codec = c
            if (!quiet) RunLog.write("audio", "Opus: кодировщик открыт, 192 кбит/с")
            true
        } catch (e: Exception) {
            broken = true
            OpusSupport.markEncoderBroken()
            RunLog.write("audio", "Opus: кодировщик не открылся: ${e.message}")
            onLog("Opus-кодировщик не открылся: ${e.message}")
            false
        }
    }

    fun encode(pcm: ShortArray, count: Int) {
        val c = codec ?: return
        if (broken || count <= 0) return
        try {
            val index = c.dequeueInputBuffer(10_000)
            if (index >= 0) {
                val buf = c.getInputBuffer(index)
                if (buf == null) {
                    c.queueInputBuffer(index, 0, 0, pts, 0)
                } else {
                    buf.clear()
                    buf.order(ByteOrder.nativeOrder())
                    val shorts = buf.asShortBuffer()
                    val n = minOf(count, shorts.remaining())
                    shorts.put(pcm, 0, n)
                    c.queueInputBuffer(index, 0, n * 2, pts, 0)
                    pts += n * 1_000_000L / AudioEngine.SAMPLE_RATE
                }
            }
            drain(c)
        } catch (e: Exception) {
            broken = true
            OpusSupport.markEncoderBroken()
            RunLog.write("audio", "Opus: кодировщик сломался: ${e.message}")
            onLog("Opus-кодировщик сломался: ${e.message}")
        }
    }

    private fun drain(c: MediaCodec) {
        while (true) {
            val index = c.dequeueOutputBuffer(info, 0)
            if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) continue
            if (index < 0) break
            var data: ByteArray? = null
            if (info.size > 0 &&
                (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0
            ) {
                val buf = c.getOutputBuffer(index)
                if (buf != null) {
                    buf.position(info.offset)
                    buf.limit(info.offset + info.size)
                    data = ByteArray(info.size)
                    buf.get(data)
                }
            }
            c.releaseOutputBuffer(index, false)
            if (data != null) onPacket(data)
        }
    }

    fun release() {
        val c = codec ?: return
        codec = null
        try {
            c.stop()
        } catch (_: Exception) {
        }
        try {
            c.release()
        } catch (_: Exception) {
        }
        if (!quiet) RunLog.write("audio", "Opus: кодировщик закрыт")
    }
}

/** Пакет Opus -> блок PCM, уже сведённый в моно. */
class OpusDecoder(
    private val channels: Int,
    private val onPcm: (ShortArray) -> Unit,
    private val onLog: (String) -> Unit,
) {
    private var codec: MediaCodec? = null
    private val info = MediaCodec.BufferInfo()
    private var pts = 0L
    private var broken = false

    /** Декодер сорвался на живом звуке — этот экземпляр больше не годится. */
    val isBroken: Boolean
        get() = broken

    fun start(): Boolean {
        if (codec != null) return true
        if (broken || !OpusSupport.decoderUsable()) return false
        val format = MediaFormat.createAudioFormat(
            OpusSupport.MIME, AudioEngine.SAMPLE_RATE, channels
        )
        // Сначала штатный софтверный декодер AOSP, затем системный по типу.
        // Вендорские декодеры на части прошивок открываются и сразу умирают.
        val swName = OpusSupport.softwareDecoderName()
        val names = if (swName != null) listOf(swName) else emptyList()
        var last: Exception? = null
        for (name in names + listOf("")) {
            var candidate: MediaCodec? = null
            try {
                candidate = if (name.isEmpty()) {
                    MediaCodec.createDecoderByType(OpusSupport.MIME)
                } else {
                    MediaCodec.createByCodecName(name)
                }
                candidate.configure(format, null, null, 0)
                candidate.start()
                codec = candidate
                val how = if (name.isEmpty()) "системный" else name
                RunLog.write("audio", "Opus: декодер открыт ($how), каналов: $channels")
                return true
            } catch (e: Exception) {
                last = e
                RunLog.write(
                    "audio",
                    "Opus: декодер «${name.ifEmpty { "системный" }}» не открылся: ${e.message}"
                )
                try {
                    candidate?.release()
                } catch (_: Exception) {
                }
            }
        }
        broken = true
        OpusSupport.markDecoderBroken()
        RunLog.write("audio", "Opus: декодер не открылся: ${last?.message}")
        onLog("Opus на этом телефоне не пошёл — перехожу на AAC")
        return false
    }

    fun decode(packet: ByteArray) {
        val c = codec ?: return
        if (broken || packet.isEmpty()) return
        try {
            val index = c.dequeueInputBuffer(10_000)
            if (index >= 0) {
                val buf = c.getInputBuffer(index)
                if (buf != null && buf.capacity() >= packet.size) {
                    buf.clear()
                    buf.put(packet)
                    c.queueInputBuffer(index, 0, packet.size, pts, 0)
                    pts += 20_000L
                } else {
                    // Пакет не влез — отдаём пустой, лишь бы кодек не залип.
                    c.queueInputBuffer(index, 0, 0, pts, 0)
                }
            }
            drain(c)
        } catch (e: Exception) {
            broken = true
            OpusSupport.markDecoderBroken()
            RunLog.write("audio", "Opus: декодер сломался: ${e.message}")
            release()
            onLog("Opus на этом телефоне не пошёл — перехожу на AAC")
        }
    }

    private fun drain(c: MediaCodec) {
        while (true) {
            val index = c.dequeueOutputBuffer(info, 0)
            if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) continue
            if (index < 0) break
            var out: ShortArray? = null
            if (info.size > 1) {
                val buf = c.getOutputBuffer(index)
                if (buf != null) {
                    buf.position(info.offset)
                    buf.limit(info.offset + info.size)
                    buf.order(ByteOrder.nativeOrder())
                    val shorts = buf.asShortBuffer()
                    val n = shorts.remaining()
                    val pcm = ShortArray(n)
                    shorts.get(pcm)
                    out = if (channels >= 2) {
                        val mono = ShortArray(n / 2)
                        for (i in mono.indices) {
                            mono[i] = ((pcm[2 * i].toInt() + pcm[2 * i + 1].toInt()) / 2).toShort()
                        }
                        mono
                    } else {
                        pcm
                    }
                }
            }
            c.releaseOutputBuffer(index, false)
            if (out != null && out.isNotEmpty()) onPcm(out)
        }
    }

    fun release() {
        val c = codec ?: return
        codec = null
        try {
            c.stop()
        } catch (_: Exception) {
        }
        try {
            c.release()
        } catch (_: Exception) {
        }
        RunLog.write("audio", "Opus: декодер закрыт")
    }
}
