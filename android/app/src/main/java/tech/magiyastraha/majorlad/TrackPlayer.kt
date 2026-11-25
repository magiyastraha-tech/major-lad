package tech.magiyastraha.majorlad

import android.content.Context
import android.media.MediaPlayer
import android.os.Environment
import java.io.File

/**
 * Простой проигрыватель: играет файлы из папок «Music/Мажорный лад»,
 * «Music/MajorLad» и из общей папки Music. Без MIDI и без настроек.
 */
class TrackPlayer(private val ctx: Context) {

    private var player: MediaPlayer? = null
    var tracks: List<File> = emptyList()
        private set
    var index = 0
        private set
    var playing = false
        private set

    private val extensions = setOf("mp3", "wav", "ogg", "flac", "m4a", "aac", "opus")

    fun scan(): Int {
        val dirs = ArrayList<File>()
        val music = Environment.getExternalStoragePublicDirectory(
            Environment.DIRECTORY_MUSIC)
        dirs.add(File(music, "Мажорный лад"))
        dirs.add(File(music, "MajorLad"))
        dirs.add(music)
        val external = ctx.getExternalFilesDir(null)
        if (external != null) dirs.add(File(external, "Music"))

        val found = ArrayList<File>()
        for (dir in dirs) {
            if (!dir.isDirectory) continue
            val files = dir.listFiles { f ->
                f.isFile && f.extension.lowercase() in extensions
            } ?: continue
            for (f in files) {
                if (found.none { it.absolutePath == f.absolutePath }) found.add(f)
            }
        }
        found.sortBy { it.name.lowercase() }

        val was = currentFile()
        tracks = found
        if (found.isEmpty()) {
            index = 0
            release()
            return 0
        }
        val again = if (was != null) {
            found.indexOfFirst { it.absolutePath == was.absolutePath }
        } else -1
        index = if (again >= 0) again else index.coerceIn(0, found.size - 1)
        return found.size
    }

    fun currentFile(): File? = tracks.getOrNull(index)

    fun currentName(): String = currentFile()?.name ?: ""

    fun hasTracks(): Boolean = tracks.isNotEmpty()

    fun play() {
        val p = ensurePlayer() ?: return
        try {
            p.start()
            playing = true
        } catch (_: Exception) {
        }
    }

    fun pause() {
        val p = player ?: return
        try {
            if (p.isPlaying) p.pause()
        } catch (_: Exception) {
        }
        playing = false
    }

    fun toggle(): Boolean {
        if (playing) pause() else play()
        return playing
    }

    fun next(auto: Boolean = false): Boolean {
        if (tracks.isEmpty()) return false
        index = (index + 1) % tracks.size
        release()
        if (auto || playing) play() else playing = false
        return true
    }

    fun prev(): Boolean {
        if (tracks.isEmpty()) return false
        index = (index - 1 + tracks.size) % tracks.size
        release()
        if (playing) play() else playing = false
        return true
    }

    fun seekBy(ms: Long) {
        val p = player ?: return
        try {
            val target = (p.currentPosition + ms).coerceIn(0, p.duration.toLong())
            p.seekTo(target.toInt())
        } catch (_: Exception) {
        }
    }

    fun seekTo(position: Int) {
        val p = player ?: return
        try {
            p.seekTo(position.coerceIn(0, p.duration))
        } catch (_: Exception) {
        }
    }

    fun position(): Int {
        val p = player ?: return 0
        return try {
            p.currentPosition
        } catch (_: Exception) {
            0
        }
    }

    fun duration(): Int {
        val p = player ?: return 0
        return try {
            p.duration
        } catch (_: Exception) {
            0
        }
    }

    fun release() {
        val p = player
        player = null
        playing = false
        if (p != null) {
            try {
                p.stop()
            } catch (_: Exception) {
            }
            p.release()
        }
    }

    private fun ensurePlayer(): MediaPlayer? {
        val file = currentFile() ?: return null
        val existing = player
        if (existing != null) return existing
        return try {
            val p = MediaPlayer()
            p.setDataSource(file.absolutePath)
            p.prepare()
            p.setOnCompletionListener {
                next(auto = true)
            }
            player = p
            p
        } catch (e: Exception) {
            player = null
            null
        }
    }
}
