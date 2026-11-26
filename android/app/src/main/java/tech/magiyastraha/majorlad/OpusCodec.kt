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
 * честнее. Нет кодировщика — шлём несжатый звук, как раньше, и ничего не
 * ломается.
 */
object OpusSupport {
    const val MIME = "audio/opus"
    const val BITRATE = 192000

    /** Телефон умеет раскодировать Opus — об этом сообщаем собеседнику. */
    val decoderAvailable: Boolean by lazy { probe(false) }

    /** Телефон умеет и закодировать; иначе отправляем несжатый звук. */
    val encoderAvailable: Boolean by lazy { probe(true) }

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
) {
    private var codec: MediaCodec? = null
    private val info = MediaCodec.BufferInfo()
    private var pts = 0L
    private var broken = false

    /** Кодировщик сломался: звук надо вернуть на несжатый поток. */
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
            RunLog.write("audio", "Opus: кодировщик открыт, 192 кбит/с")
            true
        } catch (e: Exception) {
            broken = true
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
        RunLog.write("audio", "Opus: кодировщик закрыт")
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

    fun start(): Boolean {
        if (codec != null) return true
        if (broken || !OpusSupport.decoderAvailable) return false
        return try {
            val format = MediaFormat.createAudioFormat(
                OpusSupport.MIME, AudioEngine.SAMPLE_RATE, channels
            )
            val c = MediaCodec.createDecoderByType(OpusSupport.MIME)
            c.configure(format, null, null, 0)
            c.start()
            codec = c
            RunLog.write("audio", "Opus: декодер открыт, каналов: $channels")
            true
        } catch (e: Exception) {
            broken = true
            RunLog.write("audio", "Opus: декодер не открылся: ${e.message}")
            onLog("Opus-декодер не открылся: ${e.message}")
            false
        }
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
            RunLog.write("audio", "Opus: декодер сломался: ${e.message}")
            onLog("Opus-декодер сломался: ${e.message}")
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
