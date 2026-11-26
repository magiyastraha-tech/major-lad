package tech.magiyastraha.majorlad

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * AAC-LC через MediaCodec — запасной сжатый формат.
 *
 * Несжатый звук занимает 768 кбит/с в одну сторону: канал забит, звук
 * трещит, на видео ничего не остаётся. Поэтому несжатого потока в работе
 * больше нет: если Opus на телефоне не завёлся (а он бывает битым на
 * части прошивок), звук идёт AAC-LC. AAC есть в Android с самых первых
 * версий, всегда программный и предсказуемый — в отличие от вендорских
 * декодеров Opus, которые открываются и тут же умирают.
 *
 * Пакеты сырые, без ADTS-заголовков, поэтому декодеру нужен
 * AudioSpecificConfig — 2 байта, описывающие AAC-LC 48 кГц моно.
 */
object AacSupport {
    const val MIME = "audio/mp4a-latm"
    const val BITRATE = 128000

    /** AAC-LC, 48 кГц, моно. Для стерео было бы 0x11 0x90. */
    val ASC_MONO = byteArrayOf(0x11, 0x88.toByte())

    /**
     * Эталонный кадр AAC-LC: 1024 отсчёта тишины. Кодировать пробный кадр
     * своим кодировщиком не нужно — проба декодера не должна зависеть от
     * того, умеет ли телефон кодировать.
     */
    private val PROBE_FRAME = byteArrayOf(0x01, 0x18, 0x20, 0x07)

    /** Телефон умеет раскодировать AAC — об этом сообщаем собеседнику. */
    val decoderAvailable: Boolean by lazy { probe(false) }

    @Volatile
    private var decoderBroken = false
    private var selfTested = false
    private var selfTestOk = false

    /**
     * Можно ли честно обещать собеседнику AAC прямо сейчас: кодек в
     * системе есть, эталонный кадр реально раскодировался, и декодер ещё
     * не срывался на живом звуке.
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
                        "AAC: пробный кадр раскодирован"
                    } else {
                        "AAC: пробный кадр не раскодировался"
                    }
                )
            }
        }
        return selfTestOk
    }

    /** Заявка снимается до конца занятия: собеседник сам выберет другой формат. */
    fun markDecoderBroken() {
        decoderBroken = true
    }

    private fun probeDecode(): Boolean {
        return try {
            val format = MediaFormat.createAudioFormat(MIME, AudioEngine.SAMPLE_RATE, 1).apply {
                setByteBuffer("csd-0", ByteBuffer.wrap(ASC_MONO))
            }
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
                            buf.put(PROBE_FRAME)
                            c.queueInputBuffer(i, 0, PROBE_FRAME.size, 0, 0)
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
            RunLog.write("audio", "AAC: проба декодера не прошла: ${e.message}")
            false
        }
    }

    /** Штатный софтверный декодер AOSP надёжнее вендорского. */
    fun softwareDecoderName(): String? = try {
        val format = MediaFormat.createAudioFormat(MIME, AudioEngine.SAMPLE_RATE, 1)
        val list = MediaCodecList(MediaCodecList.REGULAR_CODECS)
        val infos = list.codecInfos
        val preferred = listOf("c2.android.aac.decoder", "OMX.google.aac.decoder")
        preferred.firstOrNull { name -> infos.any { !it.isEncoder && it.name == name } }
            ?: list.findDecoderForFormat(format)
    } catch (_: Exception) {
        null
    }

    /** Телефон умеет и закодировать AAC; иначе — Opus или ничего. */
    val encoderAvailable: Boolean by lazy { probe(true) }

    @Volatile
    private var encoderBroken = false

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

/**
 * Блоки PCM -> пакеты AAC-LC. Кодировщику нужны кадры ровно по 1024
 * отсчёта, а микрофон отдаёт по 480 — остаток копится внутри.
 */
class AacEncoder(
    private val onPacket: (ByteArray) -> Unit,
    private val onLog: (String) -> Unit,
) {
    private var codec: MediaCodec? = null
    private val info = MediaCodec.BufferInfo()
    private var pts = 0L
    private var broken = false
    private val frame = ShortArray(FRAME_SAMPLES)
    private var filled = 0

    /** Кодировщик сломался: звук надо отдать другому формату. */
    val isBroken: Boolean get() = broken

    fun start(): Boolean {
        if (codec != null) return true
        if (broken || !AacSupport.encoderAvailable) return false
        return try {
            val format = MediaFormat.createAudioFormat(
                AacSupport.MIME, AudioEngine.SAMPLE_RATE, 1
            ).apply {
                setInteger(MediaFormat.KEY_AAC_PROFILE,
                    MediaCodecInfo.CodecProfileLevel.AACObjectLC)
                setInteger(MediaFormat.KEY_BIT_RATE, AacSupport.BITRATE)
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, FRAME_SAMPLES * 2)
            }
            val c = MediaCodec.createEncoderByType(AacSupport.MIME)
            c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            c.start()
            codec = c
            RunLog.write("audio", "AAC: кодировщик открыт, ${AacSupport.BITRATE / 1000} кбит/с")
            true
        } catch (e: Exception) {
            broken = true
            AacSupport.markEncoderBroken()
            RunLog.write("audio", "AAC: кодировщик не открылся: ${e.message}")
            onLog("AAC-кодировщик не открылся: ${e.message}")
            false
        }
    }

    fun encode(pcm: ShortArray, count: Int) {
        if (broken || count <= 0 || codec == null) return
        try {
            var off = 0
            while (off < count) {
                val n = minOf(count - off, FRAME_SAMPLES - filled)
                System.arraycopy(pcm, off, frame, filled, n)
                filled += n
                off += n
                if (filled == FRAME_SAMPLES) {
                    feed(frame)
                    filled = 0
                }
            }
            drain()
        } catch (e: Exception) {
            broken = true
            AacSupport.markEncoderBroken()
            RunLog.write("audio", "AAC: кодировщик сломался: ${e.message}")
            onLog("AAC-кодировщик сломался: ${e.message}")
        }
    }

    private fun feed(samples: ShortArray) {
        val c = codec ?: return
        val index = c.dequeueInputBuffer(10_000)
        if (index < 0) return
        val buf = c.getInputBuffer(index)
        if (buf == null) {
            c.queueInputBuffer(index, 0, 0, pts, 0)
            return
        }
        buf.clear()
        buf.order(ByteOrder.nativeOrder())
        val shorts = buf.asShortBuffer()
        val n = minOf(samples.size, shorts.remaining())
        shorts.put(samples, 0, n)
        c.queueInputBuffer(index, 0, n * 2, pts, 0)
        pts += n * 1_000_000L / AudioEngine.SAMPLE_RATE
    }

    private fun drain() {
        val c = codec ?: return
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
        RunLog.write("audio", "AAC: кодировщик закрыт")
    }

    private companion object {
        const val FRAME_SAMPLES = 1024
    }
}

/** Пакет AAC-LC -> блок PCM в моно. */
class AacDecoder(
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
        if (broken || !AacSupport.decoderUsable()) return false
        val format = MediaFormat.createAudioFormat(
            AacSupport.MIME, AudioEngine.SAMPLE_RATE, 1
        ).apply {
            // Без описания потока сырые кадры AAC не раскодировать.
            setByteBuffer("csd-0", ByteBuffer.wrap(AacSupport.ASC_MONO))
        }
        val swName = AacSupport.softwareDecoderName()
        val names = if (swName != null) listOf(swName) else emptyList()
        var last: Exception? = null
        for (name in names + listOf("")) {
            var candidate: MediaCodec? = null
            try {
                candidate = if (name.isEmpty()) {
                    MediaCodec.createDecoderByType(AacSupport.MIME)
                } else {
                    MediaCodec.createByCodecName(name)
                }
                candidate.configure(format, null, null, 0)
                candidate.start()
                codec = candidate
                val how = if (name.isEmpty()) "системный" else name
                RunLog.write("audio", "AAC: декодер открыт ($how)")
                return true
            } catch (e: Exception) {
                last = e
                RunLog.write(
                    "audio",
                    "AAC: декодер «${name.ifEmpty { "системный" }}» не открылся: ${e.message}"
                )
                try {
                    candidate?.release()
                } catch (_: Exception) {
                }
            }
        }
        broken = true
        AacSupport.markDecoderBroken()
        RunLog.write("audio", "AAC: декодер не открылся: ${last?.message}")
        onLog("AAC на этом телефоне не пошёл")
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
                    pts += 21_333L
                } else {
                    c.queueInputBuffer(index, 0, 0, pts, 0)
                }
            }
            drain(c)
        } catch (e: Exception) {
            broken = true
            AacSupport.markDecoderBroken()
            RunLog.write("audio", "AAC: декодер сломался: ${e.message}")
            release()
            onLog("AAC на этом телефоне не пошёл")
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
                    out = pcm
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
        RunLog.write("audio", "AAC: декодер закрыт")
    }
}
