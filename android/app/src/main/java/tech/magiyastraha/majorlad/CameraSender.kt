package tech.magiyastraha.majorlad

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Matrix
import android.graphics.Rect
import android.graphics.YuvImage
import android.util.Size
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors

/**
 * Камера шлёт собеседнику картинку примерно пятнадцать раз в секунду.
 * Кодек H.264 включается, когда собеседник его понимает: на том же потоке
 * картинка заметно чище, потому что передаётся разница между кадрами, а не
 * каждый кадр целиком. Если H.264 недоступен, идёт JPEG — он работает
 * всегда и на любом телефоне.
 */
class CameraSender(
    private val ctx: Context,
    private val owner: LifecycleOwner,
    private val onLog: (String) -> Unit,
    /** Стоит ли слать H.264: собеседник его понимает. */
    private val useH264: () -> Boolean,
    /** Готовый кусок потока и признак: true — H.264, false — JPEG. */
    private val onFrame: (ByteArray, Boolean) -> Unit,
) {
    companion object {
        /** Кадров в секунду: больше канал не держит вместе со звуком. */
        private const val H264_FPS = 15
        /** Предел потока H.264, бит в секунду. */
        private const val H264_BITRATE = 500_000
    }

    private var provider: ProcessCameraProvider? = null
    private val executor = Executors.newSingleThreadExecutor()
    @Volatile private var lastFrame = 0L
    @Volatile private var active = false
    private var encoder: H264Encoder? = null
    private var encoderRotation = -1
    private var h264On = false
    private var noEncoderLogged = false

    fun start() {
        if (active) return
        active = true
        val future = ProcessCameraProvider.getInstance(ctx)
        future.addListener({
            try {
                val p = future.get()
                provider = p
                val analysis = ImageAnalysis.Builder()
                    .setTargetResolution(Size(640, 480))
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()
                analysis.setAnalyzer(executor) { image -> analyze(image) }
                p.unbindAll()
                p.bindToLifecycle(owner, CameraSelector.DEFAULT_FRONT_CAMERA, analysis)
            } catch (e: Exception) {
                active = false
                onLog("Камера не включилась: ${e.message}")
            }
        }, ContextCompat.getMainExecutor(ctx))
    }

    fun stop() {
        active = false
        try {
            provider?.unbindAll()
        } catch (_: Exception) {
        }
        provider = null
        encoder?.release()
        encoder = null
        encoderRotation = -1
        h264On = false
    }

    private fun analyze(image: ImageProxy) {
        try {
            if (!active) return
            val now = System.currentTimeMillis()
            if (now - lastFrame < 66) return
            lastFrame = now
            if (useH264()) {
                val enc = ensureEncoder(image)
                if (enc != null) {
                    enc.encode(image, image.imageInfo.rotationDegrees)
                    return
                }
            } else if (encoder != null) {
                encoder?.release()
                encoder = null
                encoderRotation = -1
                h264On = false
                RunLog.write("video", "H.264: собеседник его не понимает — видео идёт JPEG")
            }
            val jpeg = imageToJpeg(image)
            if (jpeg != null) onFrame(jpeg, false)
        } catch (_: Exception) {
        } finally {
            image.close()
        }
    }

    /**
     * Кодировщик под текущий поворот камеры. Если телефон повернули, размер
     * кадра меняется местами — кодировщик пересоздаётся.
     */
    private fun ensureEncoder(image: ImageProxy): H264Encoder? {
        val rotation = ((image.imageInfo.rotationDegrees % 360) + 360) % 360
        val sideways = rotation == 90 || rotation == 270
        val w = if (sideways) image.height else image.width
        val h = if (sideways) image.width else image.height
        // H.264 требует чётных сторон; нечётный кадр честнее отдать JPEG.
        if (w <= 0 || h <= 0 || w % 2 != 0 || h % 2 != 0) return null
        var enc = encoder
        if (enc != null && encoderRotation != rotation) {
            enc.release()
            enc = null
            encoder = null
            h264On = false
        }
        if (enc == null) {
            if (!H264Support.encoderAvailable) {
                if (!noEncoderLogged) {
                    noEncoderLogged = true
                    RunLog.write("video", "H.264: кодировщика нет — видео идёт JPEG")
                }
                return null
            }
            enc = H264Encoder(w, h, H264_FPS, H264_BITRATE,
                { data -> onFrame(data, true) }, onLog)
            if (!enc.start()) return null
            encoder = enc
            encoderRotation = rotation
        }
        if (!h264On) {
            h264On = true
            RunLog.write("video", "H.264: $w×$h, $H264_FPS кадров/с, до ${H264_BITRATE / 1000} кбит/с")
        }
        return enc
    }

    private fun imageToJpeg(image: ImageProxy): ByteArray? {
        val w = image.width
        val h = image.height
        if (w <= 0 || h <= 0) return null
        val nv21 = yuvToNv21(image)
        val yuv = YuvImage(nv21, ImageFormat.NV21, w, h, null)
        val out = ByteArrayOutputStream()
        if (!yuv.compressToJpeg(Rect(0, 0, w, h), 55, out)) return null
        val degrees = image.imageInfo.rotationDegrees
        if (degrees == 0) return out.toByteArray()
        val raw = out.toByteArray()
        val bmp = BitmapFactory.decodeByteArray(raw, 0, raw.size) ?: return raw
        val matrix = Matrix().apply { postRotate(degrees.toFloat()) }
        val rotated = Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, matrix, true)
        val out2 = ByteArrayOutputStream()
        rotated.compress(Bitmap.CompressFormat.JPEG, 55, out2)
        return out2.toByteArray()
    }

    private fun yuvToNv21(image: ImageProxy): ByteArray {
        val w = image.width
        val h = image.height
        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]
        val ySize = w * h
        val nv21 = ByteArray(ySize + ySize / 2)

        val yBuf = yPlane.buffer
        val yRowStride = yPlane.rowStride
        val yPix = yPlane.pixelStride
        var pos = 0
        if (yPix == 1 && yRowStride == w) {
            yBuf.get(nv21, 0, ySize)
            pos = ySize
        } else {
            val row = ByteArray(yRowStride)
            for (r in 0 until h) {
                yBuf.position(r * yRowStride)
                val len = minOf(yRowStride, yBuf.remaining())
                yBuf.get(row, 0, len)
                var col = 0
                while (col < w) {
                    nv21[pos++] = row[col * yPix]
                    col++
                }
            }
        }

        val uBuf = uPlane.buffer
        val vBuf = vPlane.buffer
        val uRowStride = uPlane.rowStride
        val vRowStride = vPlane.rowStride
        val uPix = uPlane.pixelStride
        val vPix = vPlane.pixelStride
        val uRow = ByteArray(maxOf(uRowStride, 1))
        val vRow = ByteArray(maxOf(vRowStride, 1))
        val chromaRows = h / 2
        val chromaCols = w / 2
        for (r in 0 until chromaRows) {
            uBuf.position(r * uRowStride)
            vBuf.position(r * vRowStride)
            val uLen = minOf(uRowStride, uBuf.remaining())
            val vLen = minOf(vRowStride, vBuf.remaining())
            uBuf.get(uRow, 0, uLen)
            vBuf.get(vRow, 0, vLen)
            var c = 0
            while (c < chromaCols) {
                nv21[pos++] = vRow[c * vPix]
                nv21[pos++] = uRow[c * uPix]
                c++
            }
        }
        return nv21
    }
}
