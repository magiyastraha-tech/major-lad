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
 * Камера шлёт собеседнику картинку в JPEG примерно пятнадцать раз в секунду.
 * Такой поток тянет и слабый телефон, и домашний Wi-Fi.
 */
class CameraSender(
    private val ctx: Context,
    private val owner: LifecycleOwner,
    private val onLog: (String) -> Unit,
    private val onJpeg: (ByteArray) -> Unit,
) {
    private var provider: ProcessCameraProvider? = null
    private val executor = Executors.newSingleThreadExecutor()
    @Volatile private var lastFrame = 0L
    @Volatile private var active = false

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
    }

    private fun analyze(image: ImageProxy) {
        try {
            if (!active) return
            val now = System.currentTimeMillis()
            if (now - lastFrame < 66) return
            lastFrame = now
            val jpeg = imageToJpeg(image)
            if (jpeg != null) onJpeg(jpeg)
        } catch (_: Exception) {
        } finally {
            image.close()
        }
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
