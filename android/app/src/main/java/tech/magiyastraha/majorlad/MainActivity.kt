package tech.magiyastraha.majorlad

import android.Manifest
import android.app.AlertDialog
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.SeekBar
import android.widget.Spinner
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.tabs.TabLayout
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * Одно приложение — две версии. В версии преподавателя программа сама
 * открывает дверь и ждёт ученика; в версии ученика — вводит адрес и стучится.
 * Остальное одинаково: чат, громкость, плеер, видео.
 */
class MainActivity : AppCompatActivity() {

    private val isTeacher = BuildConfig.IS_TEACHER
    private val handler = Handler(Looper.getMainLooper())
    private val prefs by lazy { getSharedPreferences("majorlad", MODE_PRIVATE) }
    private val store by lazy { ChatStore(this) }
    private val player by lazy { TrackPlayer(this) }

    private var server: NetServer? = null
    private var client: NetClient? = null
    private var audio: AudioEngine? = null
    private var camera: CameraSender? = null
    private var frameCounter = 0

    private var connected = false
    private var peerMode = false

    private var statusText: TextView? = null
    private var addressText: TextView? = null
    private var videoView: ImageView? = null
    private var videoHint: TextView? = null
    private var btnMic: Button? = null
    private var btnExpand: Button? = null
    private var btnMetro: Button? = null
    private var editBpm: EditText? = null
    private var editBeats: EditText? = null

    private var btnVolMode: Button? = null
    private var volLocal: LinearLayout? = null
    private var volPeer: LinearLayout? = null
    private var seekPeer: SeekBar? = null
    private var labelPeer: TextView? = null
    private var seekTrackVol: SeekBar? = null
    private var labelTrack: TextView? = null
    private var seekMetroVol: SeekBar? = null
    private var labelMetro: TextView? = null
    private var seekOutMic: SeekBar? = null
    private var labelOutMic: TextView? = null
    private var seekOutMetro: SeekBar? = null
    private var labelOutMetro: TextView? = null

    private var trackName: TextView? = null
    private var trackList: ListView? = null
    private var btnPlay: Button? = null
    private var seekTrack: SeekBar? = null
    private var trackStatus: TextView? = null

    private var chatList: ListView? = null
    private var chatInput: EditText? = null
    private var chatAdapter: ChatAdapter? = null
    private var trackAdapter: ArrayAdapter<String>? = null
    private var chatVersion = 0
    private var addingTrack = false

    private val permLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { }
    private val openDoc = registerForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri -> uri?.let { onDocumentPicked(it) } }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        bindViews()
        setupTabs()
        setupChat()
        setupVolume()
        setupPlayer()
        setupHome()
        findViewById<Button>(R.id.btnSettings).setOnClickListener { showSettings() }
        askPermissions()
        if (isTeacher) startServer() else restoreStudentFields()
        refreshAll()
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        camera?.stop()
        camera = null
        client?.close()
        server?.stop()
        audio?.stop()
        player.release()
        super.onDestroy()
    }

    // ---- разметка ----

    private fun bindViews() {
        statusText = findViewById(R.id.statusText)
        addressText = findViewById(R.id.addressText)
        videoView = findViewById(R.id.videoView)
        videoHint = findViewById(R.id.videoHint)
        btnMic = findViewById(R.id.btnMic)
        btnExpand = findViewById(R.id.btnExpand)
        btnMetro = findViewById(R.id.btnMetro)
        editBpm = findViewById(R.id.editBpm)
        editBeats = findViewById(R.id.editBeats)

        btnVolMode = findViewById(R.id.btnVolMode)
        volLocal = findViewById(R.id.volLocal)
        volPeer = findViewById(R.id.volPeer)
        seekPeer = findViewById(R.id.seekPeer)
        labelPeer = findViewById(R.id.labelPeer)
        seekTrackVol = findViewById(R.id.seekTrackVol)
        labelTrack = findViewById(R.id.labelTrack)
        seekMetroVol = findViewById(R.id.seekMetroVol)
        labelMetro = findViewById(R.id.labelMetro)
        seekOutMic = findViewById(R.id.seekOutMic)
        labelOutMic = findViewById(R.id.labelOutMic)
        seekOutMetro = findViewById(R.id.seekOutMetro)
        labelOutMetro = findViewById(R.id.labelOutMetro)

        trackName = findViewById(R.id.trackName)
        trackList = findViewById(R.id.trackList)
        btnPlay = findViewById(R.id.btnPlay)
        seekTrack = findViewById(R.id.seekTrack)
        trackStatus = findViewById(R.id.trackStatus)

        chatList = findViewById(R.id.chatList)
        chatInput = findViewById(R.id.chatInput)

        findViewById<TextView>(R.id.headerTitle).text = "Мажорный лад"
    }

    private fun setupTabs() {
        val tabs = findViewById<TabLayout>(R.id.tabs)
        val pages = arrayOf(
            findViewById<View>(R.id.pageHome),
            findViewById<View>(R.id.pageChat),
            findViewById<View>(R.id.pageVolume),
            findViewById<View>(R.id.pagePlayer)
        )
        val names = arrayOf("Главная", "Чат", "Громкость", "Плеер")
        for (name in names) tabs.addTab(tabs.newTab().setText(name))
        tabs.addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
            override fun onTabSelected(tab: TabLayout.Tab) {
                pages.forEachIndexed { i, page ->
                    page.visibility = if (i == tab.position) View.VISIBLE else View.GONE
                }
            }

            override fun onTabUnselected(tab: TabLayout.Tab) {}
            override fun onTabReselected(tab: TabLayout.Tab) {}
        })
        pages.forEachIndexed { i, page -> page.visibility = if (i == 0) View.VISIBLE else View.GONE }
    }

    // ---- главная ----

    private fun setupHome() {
        btnMic?.setOnClickListener { toggleMic() }
        btnExpand?.setOnClickListener { expandVideo() }
        btnMetro?.setOnClickListener { toggleMetro() }
        findViewById<Button>(R.id.btnCollapse).setOnClickListener {
            findViewById<View>(R.id.fullVideoBox).visibility = View.GONE
        }
        editBpm?.setOnFocusChangeListener { _, hasFocus ->
            if (!hasFocus) applyMetroFields()
        }
        editBeats?.setOnFocusChangeListener { _, hasFocus ->
            if (!hasFocus) applyMetroFields()
        }
        (findViewById<View>(R.id.btnConnect) as? Button)?.setOnClickListener { connectStudent() }
    }

    private fun startServer() {
        val name = prefs.getString("name", "").orEmpty().ifEmpty { "Преподаватель" }
        val srv = NetServer(
            handler, name, Proto.APP_PORT,
            { text -> setStatus(text) },
            { msg -> onMessage(msg) },
            { peer -> onPeerJoined(peer) },
            { _ -> onPeerLeft() },
            { on -> onVideoState(on) },
            { bmp -> onFrame(bmp) },
            { file, path, media -> onFileReceived(file, path, media) },
            { on, bpm, beats, hello -> onPeerMetro(on, bpm, beats, hello) },
            { before, limit -> store.page(before, limit) },
            { tracksFromPlayer() },
            { name2 -> trackBytes(name2) },
            { list -> onMediaList(list) },
            { text -> setStatus(text) },
            { file, data, media -> saveIncoming(file, data, media) }
        )
        server = srv
        srv.start()
        val ip = ownWifiAddress()
        addressText?.text = if (ip != null) {
            "Мой адрес в вайфае: $ip, порт ${Proto.APP_PORT}"
        } else {
            "Вайфай не найден. Включите его — ученик подключится по адресу телефона."
        }
    }

    private fun restoreStudentFields() {
        val host = findViewById<View>(R.id.editHost) as? EditText
        val port = findViewById<View>(R.id.editPort) as? EditText
        val name = findViewById<View>(R.id.editName) as? EditText
        host?.setText(prefs.getString("host", ""))
        port?.setText(prefs.getString("port", Proto.APP_PORT.toString()))
        name?.setText(prefs.getString("name", ""))
    }

    private fun connectStudent() {
        if (connected) {
            client?.close()
            client = null
            connected = false
            camera?.stop()
            camera = null
            audio?.stop()
            audio = null
            clearVideo("Видео преподавателя")
            setStatus("Отключено")
            refreshAll()
            return
        }
        val host = findViewById<View>(R.id.editHost) as? EditText?.text?.toString()?.trim().orEmpty()
        val portText = findViewById<View>(R.id.editPort) as? EditText?.text?.toString()?.trim().orEmpty()
        val name = findViewById<View>(R.id.editName) as? EditText?.text?.toString()?.trim().orEmpty()
            .ifEmpty { "Ученик" }
        val port = portText.toIntOrNull() ?: Proto.APP_PORT
        if (host.isEmpty()) {
            setStatus("Впишите адрес преподавателя, например 192.168.1.42")
            return
        }
        prefs.edit().putString("host", host).putString("port", port.toString())
            .putString("name", name).apply()
        val c = NetClient(
            handler, name, host, port,
            { text -> setStatus(text) },
            { msg -> onMessage(msg) },
            { list -> onHistory(list) },
            { teacher -> onPeerJoined(teacher) },
            { reason -> onClientEnded(reason) },
            { on -> onVideoState(on) },
            { bmp -> onFrame(bmp) },
            { file, path, media -> onFileReceived(file, path, media) },
            { list -> onMediaList(list) },
            { text -> setStatus(text) },
            { on, bpm, beats, hello -> onPeerMetro(on, bpm, beats, hello) },
            { file, data, media -> saveIncoming(file, data, media) },
            { tracksFromPlayer() },
            { name2 -> trackBytes(name2) }
        )
        client = c
        c.connect()
        setStatus("Стучимся к $host…")
    }

    private fun onPeerJoined(peer: String) {
        connected = true
        attachAudio(10)
        updateButtons()
        setStatus(if (isTeacher) "$peer подключился" else "Связь с преподавателем есть")
        startCameraIfPossible()
    }

    /** Звук создаётся в сетевом потоке — дождёмся его, не задерживая интерфейс. */
    private fun attachAudio(tries: Int) {
        val engine = server?.audio ?: client?.audio
        if (engine != null) {
            audio = engine
            applyAudioSettings()
            updateButtons()
            return
        }
        if (tries > 0) {
            handler.postDelayed({ if (connected) attachAudio(tries - 1) }, 300)
        }
    }

    private fun onPeerLeft() {
        connected = false
        camera?.stop()
        camera = null
        audio?.stop()
        audio = null
        clearVideo(if (isTeacher) "Видео появится, когда ученик подключится" else "Видео преподавателя")
        setStatus(if (isTeacher) "Ждём ученика" else "Преподаватель отключился")
        updateButtons()
    }

    private fun onClientEnded(reason: String) {
        connected = false
        camera?.stop()
        camera = null
        audio?.stop()
        audio = null
        clearVideo("Видео преподавателя")
        setStatus(
            when (reason) {
                "rejected" -> "Преподаватель занят другим учеником"
                "closed" -> "Преподаватель завершил занятие"
                else -> "Связь потеряна"
            }
        )
        updateButtons()
    }

    private fun toggleMic() {
        val engine = audio
        if (engine == null) {
            announce(if (isTeacher) "Сначала дождитесь ученика" else "Сначала подключитесь")
            return
        }
        engine.micOn = !engine.micOn
        prefs.edit().putBoolean("micOn", engine.micOn).apply()
        updateButtons()
        announce(if (engine.micOn) "Микрофон включён" else "Микрофон выключен")
    }

    private fun toggleMetro() {
        val engine = audio
        if (engine == null) {
            announce(if (isTeacher) "Сначала дождитесь ученика" else "Сначала подключитесь")
            return
        }
        engine.metroOn = !engine.metroOn
        applyMetroFields()
        if (connected) {
            val on = engine.metroOn
            val bpm = engine.metroBpm
            val beats = engine.metroBeats
            if (isTeacher) server?.sendMetro(on, bpm, beats) else client?.sendMetro(on, bpm, beats)
        }
        updateButtons()
        announce(if (engine.metroOn) "Метроном включён" else "Метроном выключен")
    }

    private fun applyMetroFields() {
        val engine = audio ?: return
        val bpm = editBpm?.text?.toString()?.toIntOrNull()?.coerceIn(20, 300) ?: engine.metroBpm
        val beats = editBeats?.text?.toString()?.toIntOrNull()?.coerceIn(1, 16) ?: engine.metroBeats
        if (bpm == engine.metroBpm && beats == engine.metroBeats) return
        engine.metroBpm = bpm
        engine.metroBeats = beats
        editBpm?.setText(bpm.toString())
        editBeats?.setText(beats.toString())
        if (connected && engine.metroOn) {
            if (isTeacher) server?.sendMetro(true, bpm, beats) else client?.sendMetro(true, bpm, beats)
        }
    }

    private fun onPeerMetro(on: Boolean, bpm: Int, beats: Int, hello: Boolean) {
        val engine = audio ?: return
        // Собеседник сам щёлкает — свои щелчки в его сторону не шлём и не играем.
        engine.peerMetroOn = on
        if (on) {
            engine.metroBpm = bpm
            engine.metroBeats = beats
            editBpm?.setText(bpm.toString())
            editBeats?.setText(beats.toString())
        }
        if (hello) setStatus("Метроном собеседника: " + if (on) "включён" else "выключен")
        updateButtons()
    }

    private fun updateButtons() {
        val engine = audio
        val micOn = engine?.micOn ?: prefs.getBoolean("micOn", true)
        val metroOn = engine?.metroOn ?: false
        btnMic?.apply {
            text = if (isTeacher) {
                if (micOn) "Звук\nвкл" else "Звук\nвыкл"
            } else {
                "З"
            }
            contentDescription = if (micOn) "Микрофон включён" else "Микрофон выключен"
            setBackgroundResource(if (micOn) R.drawable.bg_pic_on else R.drawable.bg_pic_off)
        }
        btnMetro?.apply {
            text = if (isTeacher) {
                if (metroOn) "Метроном\nвкл" else "Метроном\nвыкл"
            } else {
                "М"
            }
            contentDescription = if (metroOn) "Метроном включён" else "Метроном выключен"
            setBackgroundResource(if (metroOn) R.drawable.bg_pic_on else R.drawable.bg_pic_off)
        }
        btnExpand?.apply {
            if (isTeacher) text = "Видео\nна весь экран"
        }
    }

    private fun expandVideo() {
        val box = findViewById<View>(R.id.fullVideoBox)
        val full = findViewById<ImageView>(R.id.fullVideo)
        if (box.visibility == View.VISIBLE) {
            box.visibility = View.GONE
        } else {
            full.setImageDrawable(videoView?.drawable)
            box.visibility = View.VISIBLE
            announce("Видео на весь экран")
        }
    }

    private fun onFrame(bmp: Bitmap) {
        frameCounter++
        if (frameCounter % 2 != 0) return // телефону хватает половины кадров
        videoView?.setImageBitmap(bmp)
        videoHint?.visibility = View.GONE
        val box = findViewById<View>(R.id.fullVideoBox)
        if (box.visibility == View.VISIBLE) {
            findViewById<ImageView>(R.id.fullVideo).setImageBitmap(bmp)
        }
    }

    private fun onVideoState(on: Boolean) {
        if (!on) {
            clearVideo(if (isTeacher) "Ученик выключил видео" else "Преподаватель выключил видео")
        } else {
            videoHint?.text = "Видео включается…"
            videoHint?.visibility = View.VISIBLE
        }
    }

    private fun clearVideo(hint: String) {
        videoView?.setImageDrawable(null)
        videoHint?.text = hint
        videoHint?.visibility = View.VISIBLE
        findViewById<ImageView>(R.id.fullVideo).setImageDrawable(null)
        findViewById<View>(R.id.fullVideoBox).visibility = View.GONE
    }

    private fun startCameraIfPossible() {
        if (camera != null) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        ) return
        val cam = CameraSender(this, this,
            { text -> setStatus(text) },
            { jpeg -> audio?.sendVideoFrame(jpeg) })
        cam.start()
        camera = cam
        if (connected) {
            if (isTeacher) server?.sendVideoState(true) else client?.sendVideoState(true)
        }
    }

    // ---- чат ----

    private fun setupChat() {
        chatAdapter = ChatAdapter()
        chatList?.adapter = chatAdapter
        findViewById<Button>(R.id.btnSend).setOnClickListener { sendChatNow() }
        chatInput?.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                sendChatNow()
                true
            } else {
                false
            }
        }
        findViewById<Button>(R.id.btnAttach).setOnClickListener {
            addingTrack = false
            openDoc.launch(arrayOf("*/*"))
        }
    }

    private fun sendChatNow() {
        val text = chatInput?.text?.toString()?.trim().orEmpty()
        if (text.isEmpty()) return
        if (!connected) {
            setStatus("Пока нет связи — сообщение не отправлено")
            return
        }
        chatInput?.setText("")
        if (isTeacher) {
            val msg = server?.sendChat(text)
            if (msg != null) onMessage(msg)
        } else {
            client?.sendChat(text)
        }
    }

    private fun onMessage(msg: ChatMsg) {
        store.add(msg)
        chatVersion++
        chatAdapter?.notifyDataSetChanged()
        chatList?.post { chatList?.setSelection((chatAdapter?.count ?: 1) - 1) }
    }

    private fun onHistory(list: List<ChatMsg>) {
        store.replaceAll(list)
        chatVersion++
        chatAdapter?.notifyDataSetChanged()
        chatList?.post { chatList?.setSelection((chatAdapter?.count ?: 1) - 1) }
    }

    private fun onDocumentPicked(uri: Uri) {
        if (addingTrack) {
            addingTrack = false
            Thread({
                try {
                    val name = queryName(uri)
                    val data = contentResolver.openInputStream(uri)?.use { it.readBytes() }
                    if (data == null || data.isEmpty()) {
                        handler.post { setStatus("Файл пустой") }
                        return@Thread
                    }
                    val ok = player.addFile(name, data)
                    handler.post {
                        if (ok) {
                            refreshTrackList()
                            setStatus("Трек добавлен: $name")
                        } else {
                            setStatus("Трек не добавлен")
                        }
                    }
                } catch (e: Exception) {
                    handler.post { setStatus("Трек не добавлен: ${e.message}") }
                }
            }, "track-add").start()
            return
        }
        Thread({
            try {
                val name = queryName(uri)
                val data = contentResolver.openInputStream(uri)?.use { it.readBytes() }
                if (data == null || data.isEmpty()) {
                    handler.post { setStatus("Файл пустой") }
                    return@Thread
                }
                if (data.size > 100 * 1024 * 1024) {
                    handler.post { setStatus("Файл больше 100 мегабайт — такой не отправить") }
                    return@Thread
                }
                if (isTeacher) server?.sendFile(name, data) else client?.sendFile(name, data)
                handler.post { setStatus("Файл отправлен: $name") }
            } catch (e: Exception) {
                handler.post { setStatus("Файл не отправлен: ${e.message}") }
            }
        }, "file-send").start()
    }

    private fun queryName(uri: Uri): String {
        var name = ""
        try {
            contentResolver.query(uri, null, null, null, null)?.use { c ->
                val idx = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                if (idx >= 0 && c.moveToFirst()) name = c.getString(idx).orEmpty()
            }
        } catch (_: Exception) {
        }
        if (name.isEmpty()) name = uri.lastPathSegment?.substringAfterLast('/').orEmpty()
        return name
    }

    private fun saveIncoming(file: String, data: ByteArray, media: Boolean): String {
        if (media) {
            val saved = FileSaver.saveToMusic(this, file, data)
            return saved?.absolutePath ?: ""
        }
        return FileSaver.saveToDownloads(this, file, data)
    }

    private fun onFileReceived(file: String, path: String, media: Boolean) {
        if (media) {
            player.scan()
            refreshTrackList()
            setStatus("Трек получен в плеер: $file")
        } else {
            setStatus("Файл сохранён в $path")
            val sys = ChatMsg(Proto.newId(), "система", "Файл получен: $file",
                System.currentTimeMillis(), false)
            onMessage(sys)
        }
    }

    private fun onMediaList(list: List<Pair<String, Long>>) {
        if (list.isEmpty()) {
            setStatus("У собеседника нет треков")
            return
        }
        val names = list.map { it.first }
        AlertDialog.Builder(this)
            .setTitle("Треки собеседника")
            .setItems(names.toTypedArray()) { _, which ->
                val name = names[which]
                if (isTeacher) server?.requestMedia(name) else client?.requestMedia(name)
                setStatus("Просим трек: $name")
            }
            .setNegativeButton("Закрыть", null)
            .show()
    }

    // ---- громкость ----

    private fun setupVolume() {
        btnVolMode?.setOnClickListener {
            peerMode = !peerMode
            volLocal?.visibility = if (peerMode) View.GONE else View.VISIBLE
            volPeer?.visibility = if (peerMode) View.VISIBLE else View.GONE
            btnVolMode?.text = if (peerMode) peerLabel() else "Моя громкость"
            announce(if (peerMode) "Настройка: ${peerLabel()}" else "Настройка: моя громкость")
        }
        seekPeer?.setOnSeekBarChangeListener(simple { v, fromUser ->
            val engine = audio
            if (engine != null) engine.playVolume = v / 100f
            prefs.edit().putInt("volPeer", v).apply()
            labelPeer?.text = "$v процентов"
        })
        seekTrackVol?.setOnSeekBarChangeListener(simple { v, fromUser ->
            player.volume = v / 100f
            prefs.edit().putInt("volTrack", v).apply()
            labelTrack?.text = "$v процентов"
        })
        seekMetroVol?.setOnSeekBarChangeListener(simple { v, fromUser ->
            val engine = audio
            if (engine != null) engine.metroVolume = v / 100f
            prefs.edit().putInt("volMetro", v).apply()
            labelMetro?.text = "$v процентов"
        })
        seekOutMic?.setOnSeekBarChangeListener(simple { v, fromUser ->
            val engine = audio
            if (engine != null) engine.micGain = v / 100f
            prefs.edit().putInt("volOutMic", v).apply()
            labelOutMic?.text = "$v процентов"
        })
        seekOutMetro?.setOnSeekBarChangeListener(simple { v, fromUser ->
            val engine = audio
            if (engine != null) engine.metroVolumeOut = v / 100f
            prefs.edit().putInt("volOutMetro", v).apply()
            labelOutMetro?.text = "$v процентов"
        })
        btnVolMode?.text = "Моя громкость"
        seekPeer?.progress = prefs.getInt("volPeer", 90)
        seekTrackVol?.progress = prefs.getInt("volTrack", 80)
        seekMetroVol?.progress = prefs.getInt("volMetro", 70)
        seekOutMic?.progress = prefs.getInt("volOutMic", 100)
        seekOutMetro?.progress = prefs.getInt("volOutMetro", 70)
        labelPeer?.text = "${seekPeer?.progress ?: 90} процентов"
        labelTrack?.text = "${seekTrackVol?.progress ?: 80} процентов"
        labelMetro?.text = "${seekMetroVol?.progress ?: 70} процентов"
        labelOutMic?.text = "${seekOutMic?.progress ?: 100} процентов"
        labelOutMetro?.text = "${seekOutMetro?.progress ?: 70} процентов"
    }

    private fun peerLabel(): String =
        if (isTeacher) "Громкость для ученика" else "Громкость для преподавателя"

    private fun applyAudioSettings() {
        val engine = audio ?: return
        engine.micOn = prefs.getBoolean("micOn", true)
        engine.playVolume = prefs.getInt("volPeer", 90) / 100f
        engine.micGain = prefs.getInt("volOutMic", 100) / 100f
        engine.metroVolume = prefs.getInt("volMetro", 70) / 100f
        engine.metroVolumeOut = prefs.getInt("volOutMetro", 70) / 100f
        player.volume = prefs.getInt("volTrack", 80) / 100f
        engine.metroBpm = editBpm?.text?.toString()?.toIntOrNull()?.coerceIn(20, 300) ?: 90
        engine.metroBeats = editBeats?.text?.toString()?.toIntOrNull()?.coerceIn(1, 16) ?: 4
        applyDevices(engine)
    }

    private fun simple(body: (Int, Boolean) -> Unit) = object : SeekBar.OnSeekBarChangeListener {
        override fun onProgressChanged(bar: SeekBar?, progress: Int, fromUser: Boolean) {
            if (fromUser) body(progress, true)
        }

        override fun onStartTrackingTouch(bar: SeekBar?) {}
        override fun onStopTrackingTouch(bar: SeekBar?) {}
    }

    // ---- плеер ----

    private fun setupPlayer() {
        player.scan()
        trackAdapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, ArrayList())
        trackList?.adapter = trackAdapter
        trackList?.setOnItemClickListener { _, _, position, _ ->
            if (player.select(position)) {
                player.play()
                refreshTrackList()
                updatePlayButton()
            }
        }
        findViewById<Button>(R.id.btnAdd).setOnClickListener {
            addingTrack = true
            openDoc.launch(arrayOf("audio/*"))
        }
        findViewById<Button>(R.id.btnDelete).setOnClickListener { deleteTrack() }
        findViewById<Button>(R.id.btnImport).setOnClickListener {
            if (!connected) {
                setStatus("Импорт возможен только при связи с собеседником")
                return@setOnClickListener
            }
            if (isTeacher) server?.requestMediaList() else client?.requestMediaList()
            setStatus("Спрашиваем у собеседника список треков")
        }
        findViewById<Button>(R.id.btnBack).setOnClickListener { player.seekBy(-5000L) }
        findViewById<Button>(R.id.btnForward).setOnClickListener { player.seekBy(5000L) }
        btnPlay?.setOnClickListener {
            player.toggle()
            updatePlayButton()
        }
        seekTrack?.setOnSeekBarChangeListener(simple { v, fromUser ->
            val dur = player.duration()
            if (dur > 0) player.seekTo(dur * v / 1000)
        })
        refreshTrackList()
        startSeekTicker()
    }

    private fun deleteTrack() {
        if (!player.hasTracks()) {
            setStatus("Треков нет")
            return
        }
        val name = player.currentName()
        AlertDialog.Builder(this)
            .setTitle("Удалить трек?")
            .setMessage(name)
            .setPositiveButton("Удалить") { _, _ ->
                if (player.deleteCurrent()) {
                    refreshTrackList()
                    setStatus("Трек удалён: $name")
                } else {
                    setStatus("Не удалось удалить трек")
                }
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun refreshTrackList() {
        val names = player.tracks.map { it.name }
        trackAdapter?.clear()
        trackAdapter?.addAll(names)
        trackAdapter?.notifyDataSetChanged()
        trackName?.text = player.currentName().ifEmpty { "Трек не выбран" }
        if (names.isEmpty()) {
            trackStatus?.text = "Треков пока нет."
        } else {
            trackStatus?.text = "Треков: ${names.size}"
        }
    }

    private fun updatePlayButton() {
        btnPlay?.text = if (player.playing) (if (isTeacher) "Стоп" else "⏹")
        else (if (isTeacher) "Играть" else "▶")
        btnPlay?.contentDescription = if (player.playing) "Остановить" else "Играть"
        btnPlay?.setBackgroundResource(
            if (player.playing) R.drawable.bg_pic_on else R.drawable.bg_pic_off
        )
    }

    private fun startSeekTicker() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (player.playing) {
                    val dur = player.duration()
                    if (dur > 0) {
                        seekTrack?.progress = player.position() * 1000 / dur
                    }
                }
                updatePlayButton()
                handler.postDelayed(this, 700)
            }
        }, 700)
    }

    private fun tracksFromPlayer(): List<Pair<String, Long>> =
        player.tracks.map { it.name to it.length() }

    private fun trackBytes(name: String): ByteArray? {
        val file = player.tracks.firstOrNull { it.name == name } ?: return null
        return try {
            file.readBytes()
        } catch (_: Exception) {
            null
        }
    }

    // ---- настройки ----

    private fun showSettings() {
        val view = layoutInflater.inflate(R.layout.dialog_settings, null)
        val nameEdit = view.findViewById<EditText>(R.id.editName)
        val hostEdit = view.findViewById<EditText>(R.id.editHost)
        val portEdit = view.findViewById<EditText>(R.id.editPort)
        val ipRow = view.findViewById<View>(R.id.ipRow)
        nameEdit.setText(prefs.getString("name", ""))
        hostEdit.setText(prefs.getString("host", ""))
        portEdit.setText(prefs.getString("port", Proto.APP_PORT.toString()))
        if (isTeacher) ipRow.visibility = View.GONE

        val devices = audioDevices()
        val micDevices = devices.first
        val outDevices = devices.second
        val micSpinner = view.findViewById<Spinner>(R.id.spinMic)
        val outSpinner = view.findViewById<Spinner>(R.id.spinOut)
        val micLabels = micDevices.map { it.second }
        val outLabels = outDevices.map { it.second }
        micSpinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, micLabels)
        outSpinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, outLabels)
        micSpinner.setSelection(indexOfType(micDevices, prefs.getInt("micType", -1)))
        outSpinner.setSelection(indexOfType(outDevices, prefs.getInt("outType", -1)))

        AlertDialog.Builder(this)
            .setTitle("Настройки")
            .setView(view)
            .setPositiveButton("Сохранить") { _, _ ->
                prefs.edit()
                    .putString("name", nameEdit.text.toString().trim())
                    .putString("host", hostEdit.text.toString().trim())
                    .putString("port", portEdit.text.toString().trim())
                    .putInt("micType", micDevices.getOrNull(micSpinner.selectedItemPosition)?.first ?: -1)
                    .putInt("outType", outDevices.getOrNull(outSpinner.selectedItemPosition)?.first ?: -1)
                    .apply()
                val t = findViewById<TextView>(R.id.headerTitle)
                val n = nameEdit.text.toString().trim()
                if (n.isNotEmpty()) t.text = "Мажорный лад — $n"
                audio?.let { applyDevices(it) }
                setStatus("Настройки сохранены")
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun audioDevices(): Pair<List<Pair<Int, String>>, List<Pair<Int, String>>> {
        val am = getSystemService(AUDIO_SERVICE) as AudioManager
        val inputs = ArrayList<Pair<Int, String>>()
        val outputs = ArrayList<Pair<Int, String>>()
        inputs.add(-1 to "Автоматически")
        outputs.add(-1 to "Автоматически")
        try {
            for (d in am.getDevices(AudioManager.GET_DEVICES_INPUTS)) {
                inputs.add(d.type to inputName(d.type))
            }
        } catch (_: Exception) {
        }
        try {
            for (d in am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
                outputs.add(d.type to outputName(d.type))
            }
        } catch (_: Exception) {
        }
        return inputs to outputs
    }

    private fun indexOfType(list: List<Pair<Int, String>>, type: Int): Int {
        val i = list.indexOfFirst { it.first == type }
        return if (i < 0) 0 else i
    }

    private fun inputName(type: Int): String = when (type) {
        AudioDeviceInfo.TYPE_BUILTIN_MIC -> "Встроенный микрофон"
        AudioDeviceInfo.TYPE_WIRED_HEADSET -> "Гарнитура, провод"
        AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_DEVICE ->
            "USB-гарнитура"
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "Bluetooth-гарнитура"
        else -> "Микрофон $type"
    }

    private fun outputName(type: Int): String = when (type) {
        AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "Динамик телефона"
        AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "Телефон у уха"
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES, AudioDeviceInfo.TYPE_WIRED_HEADSET ->
            "Наушники, провод"
        AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_DEVICE ->
            "USB-наушники"
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, AudioDeviceInfo.TYPE_BLUETOOTH_SCO ->
            "Bluetooth-наушники"
        else -> "Выход $type"
    }

    private fun applyDevices(engine: AudioEngine) {
        val am = getSystemService(AUDIO_SERVICE) as AudioManager
        val micType = prefs.getInt("micType", -1)
        val outType = prefs.getInt("outType", -1)
        try {
            val mic = if (micType < 0) null else
                am.getDevices(AudioManager.GET_DEVICES_INPUTS).firstOrNull { it.type == micType }
            engine.setMicDevice(mic)
        } catch (_: Exception) {
        }
        try {
            val out = if (outType < 0) null else
                am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull { it.type == outType }
            engine.setOutDevice(out)
        } catch (_: Exception) {
        }
    }

    // ---- общее ----

    private fun refreshAll() {
        chatAdapter?.notifyDataSetChanged()
        refreshTrackList()
        updateButtons()
        updatePlayButton()
    }

    private fun setStatus(text: String) {
        handler.post {
            statusText?.text = text
            statusText?.announceForAccessibility(text)
        }
    }

    private fun announce(text: String) {
        handler.post { statusText?.announceForAccessibility(text) }
    }

    private fun askPermissions() {
        val wanted = ArrayList<String>()
        wanted.add(Manifest.permission.RECORD_AUDIO)
        wanted.add(Manifest.permission.CAMERA)
        if (Build.VERSION.SDK_INT >= 33) {
            wanted.add(Manifest.permission.READ_MEDIA_AUDIO)
        } else {
            wanted.add(Manifest.permission.READ_EXTERNAL_STORAGE)
            if (Build.VERSION.SDK_INT <= 28) wanted.add(Manifest.permission.WRITE_EXTERNAL_STORAGE)
        }
        val missing = wanted.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) permLauncher.launch(missing.toTypedArray())
    }

    private fun ownWifiAddress(): String? {
        try {
            for (iface in NetworkInterface.getNetworkInterfaces()) {
                if (!iface.isUp || iface.isLoopback) continue
                for (addr in iface.inetAddresses) {
                    if (addr is Inet4Address && !addr.isLoopbackAddress) {
                        return addr.hostAddress
                    }
                }
            }
        } catch (_: Exception) {
        }
        return null
    }

    // ---- список сообщений ----

    private inner class ChatAdapter : BaseAdapter() {

        private class Row(val text: String, val header: Boolean, val mine: Boolean)

        private var rows: List<Row> = emptyList()
        private var stamp = -1

        override fun getCount(): Int {
            rebuildIfNeeded()
            return rows.size
        }

        override fun getItem(position: Int): Any = rows[position]

        override fun getItemId(position: Int): Long = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup?): View {
            rebuildIfNeeded()
            val row = rows[position]
            val v = convertView ?: layoutInflater.inflate(R.layout.item_chat, parent, false)
            val tv = v as TextView
            tv.text = row.text
            tv.setTextColor(
                when {
                    row.header -> 0xffe8b04b.toInt()
                    row.mine -> 0xffcfe3ff.toInt()
                    else -> 0xffececf4.toInt()
                }
            )
            tv.setTextSize(if (row.header) 15f else 16f)
            tv.alpha = if (row.header) 1f else 0.98f
            tv.isFocusable = true
            return v
        }

        private fun rebuildIfNeeded() {
            val stampNow = chatVersion
            if (stampNow == stamp) return
            stamp = stampNow
            val list = ArrayList<Row>()
            var lastDay = ""
            for (m in store.all) {
                val day = Proto.dayKey(m.time)
                if (day != lastDay) {
                    lastDay = day
                    list.add(Row(Proto.dateText(m.time), true, false))
                }
                val who = if (m.from.isEmpty()) "" else "${m.from}: "
                list.add(Row("$who${m.text}\n${Proto.timeText(m.time)}", false, m.mine))
            }
            rows = list
        }
    }
}
