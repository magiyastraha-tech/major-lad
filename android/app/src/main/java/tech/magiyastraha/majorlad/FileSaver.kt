package tech.magiyastraha.majorlad

import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import java.io.File

/** Принятые файлы кладём в папку «Загрузки» телефона. */
object FileSaver {

    fun saveToDownloads(ctx: Context, name: String, data: ByteArray): String {
        val safe = name.replace('/', '_').replace('\\', '_').ifEmpty { "файл" }
        if (Build.VERSION.SDK_INT >= 29) {
            try {
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, safe)
                    put(MediaStore.Downloads.MIME_TYPE, mimeOf(safe))
                    put(MediaStore.Downloads.IS_PENDING, 1)
                }
                val resolver = ctx.contentResolver
                val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                if (uri != null) {
                    resolver.openOutputStream(uri)?.use { it.write(data) }
                    values.clear()
                    values.put(MediaStore.Downloads.IS_PENDING, 0)
                    resolver.update(uri, values, null, null)
                    return "Загрузки/$safe"
                }
            } catch (_: Exception) {
            }
        }
        return try {
            val dir = File(Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DOWNLOADS), "")
            dir.mkdirs()
            val target = File(dir, safe)
            target.writeBytes(data)
            target.absolutePath
        } catch (e: Exception) {
            val dir = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "Downloads")
            dir.mkdirs()
            val target = File(dir, safe)
            target.writeBytes(data)
            target.absolutePath
        }
    }

    fun saveToMusic(ctx: Context, name: String, data: ByteArray): File? {
        val safe = name.replace('/', '_').replace('\\', '_').ifEmpty { "трек" }
        val dirs = ArrayList<File>()
        dirs.add(File(Environment.getExternalStoragePublicDirectory(
            Environment.DIRECTORY_MUSIC), "Мажорный лад"))
        val external = ctx.getExternalFilesDir(null)
        if (external != null) dirs.add(File(external, "Music/Мажорный лад"))
        dirs.add(File(ctx.filesDir, "Music"))
        for (dir in dirs) {
            try {
                dir.mkdirs()
                if (!dir.isDirectory || !dir.canWrite()) continue
                val target = File(dir, safe)
                target.writeBytes(data)
                return target
            } catch (_: Exception) {
            }
        }
        return null
    }

    private fun mimeOf(name: String): String = when (name.substringAfterLast('.', "").lowercase()) {
        "mp3" -> "audio/mpeg"
        "wav" -> "audio/wav"
        "ogg", "opus" -> "audio/ogg"
        "flac" -> "audio/flac"
        "m4a", "aac" -> "audio/mp4"
        "jpg", "jpeg" -> "image/jpeg"
        "png" -> "image/png"
        "pdf" -> "application/pdf"
        else -> "application/octet-stream"
    }
}
