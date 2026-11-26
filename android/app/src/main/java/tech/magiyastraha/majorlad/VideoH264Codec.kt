package tech.magiyastraha.majorlad

import android.graphics.Bitmap
import android.graphics.ImageFormat
import android.graphics.PixelFormat
import android.media.Image
import android.media.ImageReader
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import androidx.camera.core.ImageProxy
import java.nio.ByteBuffer

/**
 * Видео кодеком H.264 через MediaCodec.
 *
 * JPEG шлёт каждый кадр с нуля. H.264 передаёт только разницу с предыдущим
 * кадром: неподвижная гитара и руки почти не занимают канал, а на том же
 * потоке картинка заметно чище. Кодировщик и декодер H.264 есть в каждом
 * Android с первой версии, поэтому тут нет запасного варианта на уровне
 * системы — только проверка, что кодек вообще нашёлся.
 *
 * Телефон присылает кадр в YUV_420_888 повёрнутым на 90 или 270 градусов
 * (камера смотрит сбоку). H.264 не умеет поворот флагами, поэтому кадр
 * разворачивается вручную при копировании в буфер кодировщика — это
 * дешевле, чем гонять Bitmap туда-обратно.
 */
object H264Support {
    const val MIME = "video/avc"

    val encoderAvailable: Boolean by lazy { probe(true) }
    val decoderAvailable: Boolean by lazy { probe(false) }

    private fun probe(encode: Boolean): Boolean = try {
        val format = MediaFormat.createVideoFormat(MIME, 640, 480)
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

/** Есть ли в куске потока ключевой кадр (IDR, тип 5). */
fun hasIdr(data: ByteArray): Boolean {
    var i = 0
    while (i + 3 < data.size) {
        val isLong = data[i] == 0.toByte() && data[i + 1] == 0.toByte() &&
            data[i + 2] == 0.toByte() && data[i + 3] == 1.toByte()
        val isShort = data[i] == 0.toByte() && data[i + 1] == 0.toByte() &&
            data[i + 2] == 1.toByte()
        val at = when {
            isLong -> i + 4
            isShort -> i + 3
            else -> {
                i++
                continue
            }
        }
        if (at < data.size && (data[at].toInt() and 0x1f) == 5) return true
        i = at
    }
    return false
}

/** Кодировщик: кадр камеры -> кусок потока H.264 в Annex-B. */
class H264Encoder(
    private val width: Int,
    private val height: Int,
    private val fps: Int,
    private val bitrate: Int,
    private val onPacket: (ByteArray) -> Unit,
    private val onLog: (String) -> Unit,
) {
    private var codec: MediaCodec? = null
    private val info = MediaCodec.BufferInfo()
    private var pts = 0L
    private var csd: ByteArray? = null
    private var broken = false

    fun start(): Boolean {
        if (codec != null) return true
        if (broken || !H264Support.encoderAvailable) return false
        return try {
            val format = MediaFormat.createVideoFormat(
                H264Support.MIME, width, height
            ).apply {
                setInteger(
                    MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible
                )
                setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
                setInteger(MediaFormat.KEY_FRAME_RATE, fps)
                // Ключевой кадр раз в секунду: потерялся кусок — собеседник
                // ждёт не дольше секунды, а не до конца урока.
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
            }
            val c = MediaCodec.createEncoderByType(H264Support.MIME)
            c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            c.start()
            codec = c
            RunLog.write("video", "H.264: кодировщик ${width}×$height, $fps кадров/с")
            true
        } catch (e: Exception) {
            broken = true
            RunLog.write("video", "H.264: кодировщик не открылся: ${e.message}")
            onLog("H.264-кодировщик не открылся: ${e.message}")
            false
        }
    }

    fun encode(source: ImageProxy, rotation: Int) {
        val c = codec ?: return
        if (broken) return
        try {
            val index = c.dequeueInputBuffer(10_000)
            if (index < 0) return
            val target = c.getInputImage(index)
            if (target == null) {
                c.queueInputBuffer(index, 0, 0, pts, 0)
                return
            }
            copyRotated(source, target, rotation)
            c.queueInputBuffer(index, 0, width * height * 3 / 2, pts, 0)
            pts += 1_000_000L / fps
            drain(c)
        } catch (e: Exception) {
            broken = true
            RunLog.write("video", "H.264: кодировщик сломался: ${e.message}")
            onLog("H.264-кодировщик сломался: ${e.message}")
        }
    }

    private fun copyRotated(source: ImageProxy, target: Image, rotation: Int) {
        val r = ((rotation % 360) + 360) % 360
        val sp = source.planes
        val tp = target.planes
        copyPlane(sp[0], tp[0].buffer, tp[0].rowStride, tp[0].pixelStride,
            source.width, source.height, r)
        val cw = source.width / 2
        val ch = source.height / 2
        copyPlane(sp[1], tp[1].buffer, tp[1].rowStride, tp[1].pixelStride, cw, ch, r)
        copyPlane(sp[2], tp[2].buffer, tp[2].rowStride, tp[2].pixelStride, cw, ch, r)
    }

    /**
     * Копирование с поворотом. w и h — размеры исходной плоскости; при
     * повороте на 90 или 270 стороны меняются местами, поэтому по строкам
     * приёмника идёт ширина источника.
     */
    private fun copyPlane(
        srcPlane: ImageProxy.PlaneProxy, dst: ByteBuffer,
        dstRowStride: Int, dstPixStride: Int,
        w: Int, h: Int, r: Int,
    ) {
        val src = srcPlane.buffer
        val srcRowStride = srcPlane.rowStride
        val srcPixStride = srcPlane.pixelStride
        when (r) {
            90 -> for (dy in 0 until w) {
                val dRow = dy * dstRowStride
                for (dx in 0 until h) {
                    dst.put(dRow + dx * dstPixStride,
                        src.get(dy * srcRowStride + (h - 1 - dx) * srcPixStride))
                }
            }

            180 -> for (dy in 0 until h) {
                val dRow = dy * dstRowStride
                val sRow = (h - 1 - dy) * srcRowStride
                for (dx in 0 until w) {
                    dst.put(dRow + dx * dstPixStride,
                        src.get(sRow + (w - 1 - dx) * srcPixStride))
                }
            }

            270 -> for (dy in 0 until w) {
                val dRow = dy * dstRowStride
                val sCol = (w - 1 - dy) * srcPixStride
                for (dx in 0 until h) {
                    dst.put(dRow + dx * dstPixStride,
                        src.get(dx * srcRowStride + sCol))
                }
            }

            else -> for (dy in 0 until h) {
                val dRow = dy * dstRowStride
                val sRow = dy * srcRowStride
                for (dx in 0 until w) {
                    dst.put(dRow + dx * dstPixStride,
                        src.get(sRow + dx * srcPixStride))
                }
            }
        }
    }

    private fun drain(c: MediaCodec) {
        while (true) {
            val index = c.dequeueOutputBuffer(info, 0)
            if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                readCsd(c.outputFormat)
                continue
            }
            if (index < 0) break
            var data: ByteArray? = null
            if (info.size > 0) {
                val buf = c.getOutputBuffer(index)
                if (buf != null) {
                    buf.position(info.offset)
                    buf.limit(info.offset + info.size)
                    val bytes = ByteArray(info.size)
                    buf.get(bytes)
                    data = when {
                        (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0 -> {
                            if (csd == null) csd = bytes
                            null
                        }
                        // Некоторые кодеки уже кладут SPS/PPS в сам
                        // ключевой кадр — второй раз не добавляем.
                        hasIdr(bytes) -> {
                            val head = csd
                            if (head != null && !startsWith(bytes, head)) head + bytes
                            else bytes
                        }

                        else -> bytes
                    }
                }
            }
            c.releaseOutputBuffer(index, false)
            if (data != null) onPacket(data)
        }
    }

    private fun readCsd(format: MediaFormat) {
        val b0 = format.getByteBuffer("csd-0") ?: return
        val a = ByteArray(b0.remaining())
        b0.get(a)
        val b1 = format.getByteBuffer("csd-1")
        csd = if (b1 != null) {
            val c = ByteArray(b1.remaining())
            b1.get(c)
            a + c
        } else {
            a
        }
    }

    private fun startsWith(data: ByteArray, head: ByteArray): Boolean {
        if (data.size < head.size) return false
        for (i in head.indices) {
            if (data[i] != head[i]) return false
        }
        return true
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
        RunLog.write("video", "H.264: кодировщик закрыт")
    }
}

/**
 * Декодер: кусок потока H.264 -> Bitmap для окна.
 *
 * Размер картинки заранее неизвестен — он лежит в SPS, первом служебном
 * куске потока. Из него же собирается окно вывода: ImageReader с чужим
 * размером отдаёт пустые кадры. Поэтому декодер открывается при первом
 * ключевом кадре, когда SPS уже пришёл.
 */
class H264Decoder(
    private val onFrame: (Bitmap) -> Unit,
    private val onLog: (String) -> Unit,
) {
    private var codec: MediaCodec? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private val info = MediaCodec.BufferInfo()
    private var pts = 0L
    private var needKey = true
    private var broken = false

    fun feed(data: ByteArray) {
        if (broken) return
        var c = codec
        if (c == null) {
            val size = parseSpsSize(data)
            if (size == null) {
                RunLog.write("video", "H.264: в куске нет SPS — размер картинки неизвестен, ждём ключевой кадр")
                return
            }
            if (!start(size[0], size[1])) return
            c = codec ?: return
        }
        if (needKey) {
            if (!hasIdr(data)) return
            needKey = false
            try {
                c.flush()
            } catch (_: Exception) {
            }
        }
        try {
            val index = c.dequeueInputBuffer(10_000)
            if (index < 0) return
            val buf = c.getInputBuffer(index)
            if (buf == null || buf.capacity() < data.size) {
                c.queueInputBuffer(index, 0, 0, pts, 0)
                needKey = true
                return
            }
            buf.clear()
            buf.put(data)
            c.queueInputBuffer(index, 0, data.size, pts, 0)
            pts += 20_000L
            drain(c)
        } catch (_: Exception) {
            // Битая картинка: ждём следующий ключевой кадр.
            reset()
        }
    }

    private fun start(w: Int, h: Int): Boolean {
        return try {
            val r = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 4)
            val t = HandlerThread("ml-h264-dec").apply { start() }
            val handler = Handler(t.looper)
            r.setOnImageAvailableListener({ ir -> consume(ir) }, handler)
            val format = MediaFormat.createVideoFormat(H264Support.MIME, w, h)
            val c = MediaCodec.createDecoderByType(H264Support.MIME)
            c.configure(format, r.surface, null, 0)
            c.start()
            codec = c
            reader = r
            thread = t
            needKey = true
            RunLog.write("video", "H.264: декодер ${w}×$h")
            true
        } catch (e: Exception) {
            broken = true
            RunLog.write("video", "H.264: декодер не открылся: ${e.message}")
            onLog("H.264-декодер не открылся: ${e.message}")
            false
        }
    }

    private fun drain(c: MediaCodec) {
        while (true) {
            val index = c.dequeueOutputBuffer(info, 0)
            if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) continue
            if (index < 0) break
            c.releaseOutputBuffer(index, true)
        }
    }

    private fun consume(ir: ImageReader) {
        try {
            val image = ir.acquireLatestImage() ?: return
            try {
                onFrame(toBitmap(image))
            } finally {
                image.close()
            }
        } catch (_: Exception) {
        }
    }

    private fun toBitmap(image: Image): Bitmap {
        if (image.format == ImageFormat.YUV_420_888) return yuvToBitmap(image)
        val w = image.width
        val h = image.height
        val plane = image.planes[0]
        val buffer = plane.buffer
        val rowStride = plane.rowStride
        if (rowStride == w * 4) {
            val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
            buffer.rewind()
            bmp.copyPixelsFromBuffer(buffer)
            return bmp
        }
        // Строки с выравниванием: широкая картинка, потом обрезаем.
        val wide = Bitmap.createBitmap(rowStride / 4, h, Bitmap.Config.ARGB_8888)
        buffer.rewind()
        wide.copyPixelsFromBuffer(buffer)
        val cropped = Bitmap.createBitmap(wide, 0, 0, w, h)
        if (cropped !== wide) wide.recycle()
        return cropped
    }

    private fun yuvToBitmap(image: Image): Bitmap {
        val w = image.width
        val h = image.height
        val yP = image.planes[0]
        val uP = image.planes[1]
        val vP = image.planes[2]
        val yb = yP.buffer
        val ub = uP.buffer
        val vb = vP.buffer
        val pixels = IntArray(w * h)
        var p = 0
        for (j in 0 until h) {
            val yRow = j * yP.rowStride
            val uvRow = (j / 2) * uP.rowStride
            for (i in 0 until w) {
                val y = (yb.get(yRow + i * yP.pixelStride).toInt() and 0xff) - 16
                val u = (ub.get(uvRow + (i / 2) * uP.pixelStride).toInt() and 0xff) - 128
                val v = (vb.get(uvRow + (i / 2) * vP.pixelStride).toInt() and 0xff) - 128
                val r = (298 * y + 409 * v + 128) shr 8
                val g = (298 * y - 100 * u - 208 * v + 128) shr 8
                val b = (298 * y + 516 * u + 128) shr 8
                pixels[p++] = (0xff shl 24) or
                    (r.coerceIn(0, 255) shl 16) or
                    (g.coerceIn(0, 255) shl 8) or
                    b.coerceIn(0, 255)
            }
        }
        return Bitmap.createBitmap(pixels, w, h, Bitmap.Config.ARGB_8888)
    }

    /** После потери куска потока ждём ключевой кадр. */
    fun reset() {
        needKey = true
        try {
            codec?.flush()
        } catch (_: Exception) {
        }
    }

    fun release() {
        val c = codec
        codec = null
        try {
            c?.stop()
        } catch (_: Exception) {
        }
        try {
            c?.release()
        } catch (_: Exception) {
        }
        try {
            reader?.close()
        } catch (_: Exception) {
        }
        reader = null
        try {
            thread?.quitSafely()
        } catch (_: Exception) {
        }
        thread = null
        RunLog.write("video", "H.264: декодер закрыт")
    }
}

// Разбор SPS: только чтобы узнать ширину и высоту картинки.

private val HIGH_PROFILES = setOf(100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)

private class BitReader(private val data: ByteArray, private var pos: Int, private val end: Int) {
    private var bit = 0

    fun readBit(): Int {
        if (pos >= end) return 0
        val v = (data[pos].toInt() shr (7 - bit)) and 1
        bit++
        if (bit == 8) {
            bit = 0
            pos++
        }
        return v
    }

    fun readBits(n: Int): Int {
        var v = 0
        repeat(n) { v = (v shl 1) or readBit() }
        return v
    }

    fun ue(): Int {
        var zeros = 0
        while (readBit() == 0) {
            zeros++
            if (zeros > 31) return 0
        }
        if (zeros == 0) return 0
        var v = 1
        repeat(zeros) { v = (v shl 1) or readBit() }
        return v - 1
    }

    fun se(): Int {
        val k = ue()
        return if (k and 1 == 0) -(k / 2) else (k + 1) / 2
    }
}

private fun skipScalingList(r: BitReader, size: Int) {
    var last = 8
    var next = 8
    for (i in 0 until size) {
        if (r.readBit() == 1) {
            next = (last + r.se() + 256) % 256
        }
        if (next != 0) last = next
    }
}

/** Размер картинки из первого SPS в куске потока; null — SPS не нашёлся. */
private fun parseSpsSize(data: ByteArray): IntArray? {
    val starts = ArrayList<Int>()
    var i = 0
    while (i + 2 < data.size) {
        if (data[i] == 0.toByte() && data[i + 1] == 0.toByte() &&
            data[i + 2] == 1.toByte()
        ) {
            starts.add(i + 3)
            i += 3
        } else {
            i++
        }
    }
    for (k in starts.indices) {
        val at = starts[k]
        if (at >= data.size) continue
        if ((data[at].toInt() and 0x1f) != 7) continue
        val end = if (k + 1 < starts.size) starts[k + 1] - 3 else data.size
        return parseSps(data, at + 1, end)
    }
    return null
}

private fun parseSps(data: ByteArray, from: Int, end: Int): IntArray? {
    if (end - from < 4) return null
    val r = BitReader(data, from, end)
    val profile = r.readBits(8)
    r.readBits(8) // constraint flags
    r.readBits(8) // level
    r.ue() // sps id
    var chroma = 1
    if (profile in HIGH_PROFILES) {
        chroma = r.ue()
        if (chroma == 3) r.readBit()
        r.ue() // bit depth luma
        r.ue() // bit depth chroma
        r.readBit() // qpprime
        if (r.readBit() == 1) {
            val lists = if (chroma == 3) 12 else 8
            for (n in 0 until lists) {
                if (r.readBit() == 1) skipScalingList(r, if (n < 6) 16 else 64)
            }
        }
    }
    r.ue() // log2_max_frame_num_minus4
    when (r.ue()) {
        0 -> r.ue()
        1 -> {
            r.readBit()
            r.se()
            r.se()
            val n = r.ue()
            for (j in 0 until minOf(n, 256)) r.se()
        }
    }
    r.ue() // max_num_ref_frames
    r.readBit() // gaps_in_frame_num
    val wMbs = r.ue() + 1
    val hMap = r.ue() + 1
    val frameOnly = r.readBit()
    if (frameOnly == 0) r.readBit()
    r.readBit() // direct_8x8_inference
    var cl = 0
    var cr = 0
    var ct = 0
    var cb = 0
    if (r.readBit() == 1) {
        cl = r.ue()
        cr = r.ue()
        ct = r.ue()
        cb = r.ue()
    }
    val subW = if (chroma == 3) 1 else 2
    val subH = if (chroma == 1) 2 else 1
    val width = wMbs * 16 - (cl + cr) * subW
    val height = (2 - frameOnly) * hMap * 16 - (ct + cb) * subH
    if (width < 16 || height < 16 || width > 4096 || height > 4096) return null
    return intArrayOf(width, height)
}
