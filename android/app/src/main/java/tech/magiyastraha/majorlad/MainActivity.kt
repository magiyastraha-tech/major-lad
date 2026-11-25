package tech.magiyastraha.majorlad

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.EditorInfo
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.ListView
import android.widget.SeekBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.google.android.material.tabs.TabLayout

class MainActivity : AppCompatActivity() {

    companion object {
        private const val REQ_PERMS = 10
        private const val PREFS = "majorlad"
    }

    private val handler = Handler(Looper.getMainLooper())

    private lateinit var pages: List<View>
    private lateinit var statusText: TextView
    private lateinit var connectPanel: View
    private lateinit var editHost: EditText
    private lateinit var editName: EditText
    private lateinit var btnConnect: Button
    private lateinit var videoView: ImageView
    private lateinit var videoHint: TextView
    private lateinit var btnMic: Button
    private lateinit var btnExpand: Button
    private lateinit var fullVideoBox: FrameLayout
    private lateinit var fullVideo: ImageView

    private lateinit var chatList: ListView
    private lateinit var chatInput: EditText
    private lateinit var chatItems: ArrayList<String>
    private lateinit var chatAdapter: ArrayAdapter<String>

    private lateinit var labelPlay: TextView
    private lateinit var seekPlay: SeekBar
    private lateinit var labelMic: TextView
    private lateinit var seekMic: SeekBar

    private lateinit var trackName: TextView
    private lateinit var seekTrack: SeekBar
    private lateinit var btnPlay: Button
    private lateinit var trackStatus: TextView

    private var client: NetClient? = null
    private lateinit var tracks: TrackPlayer
    private var scrubbing = false
    private var playerPageActive = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val tabs = findViewById<TabLayout>(R.id.tabs)
        pages = listOf(
            findViewById(R.id.pageHome),
            findViewById(R.id.pageChat),
            findViewById(R.id.pageVolume),
            findViewById(R.id.pagePlayer))
        tabs.addTab(tabs.newTab().setText("Главная"))
        tabs.addTab(tabs.newTab().setText("Чат"))
        tabs.addTab(tabs.newTab().setText("Громкость"))
        tabs.addTab(tabs.newTab().setText("Плеер"))
        tabs.addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
            override fun onTabSelected(tab: TabLayout.Tab) = showPage(tab.position)
            override fun onTabUnselected(tab: TabLayout.Tab) {}
            override fun onTabReselected(tab: TabLayout.Tab) {}
        })
        showPage(0)

        statusText = findViewById(R.id.statusText)
        connectPanel = findViewById(R.id.connectPanel)
        editHost = findViewById(R.id.editHost)
        editName = findViewById(R.id.editName)
        btnConnect = findViewById(R.id.btnConnect)
        videoView = findViewById(R.id.videoView)
        videoHint = findViewById(R.id.videoHint)
        btnMic = findViewById(R.id.btnMic)
        btnExpand = findViewById(R.id.btnExpand)
        fullVideoBox = findViewById(R.id.fullVideoBox)
        fullVideo = findViewById(R.id.fullVideo)

        chatList = findViewById(R.id.chatList)
        chatInput = findViewById(R.id.chatInput)
        chatItems = ArrayList()
        chatAdapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, chatItems)
        chatList.adapter = chatAdapter

        labelPlay = findViewById(R.id.labelPlay)
        seekPlay = findViewById(R.id.seekPlay)
        labelMic = findViewById(R.id.labelMic)
        seekMic = findViewById(R.id.seekMic)

        trackName = findViewById(R.id.trackName)
        seekTrack = findViewById(R.id.seekTrack)
        btnPlay = findViewById(R.id.btnPlay)
        trackStatus = findViewById(R.id.trackStatus)

        tracks = TrackPlayer(this)
        tracks.scan()

        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        editHost.setText(prefs.getString("host", ""))
        editName.setText(prefs.getString("name", ""))

        val savedPlay = prefs.getInt("playVol", 100)
        val savedMic = prefs.getInt("micVol", 100)
        seekPlay.progress = savedPlay
        seekMic.progress = savedMic
        labelPlay.text = "$savedPlay%"
        labelMic.text = "$savedMic%"

        btnConnect.setOnClickListener { toggleConnect() }
        btnMic.setOnClickListener { toggleMic() }
        btnExpand.setOnClickListener { expandVideo(true) }
        findViewById<Button>(R.id.btnCollapse).setOnClickListener { expandVideo(false) }

        findViewById<Button>(R.id.btnSend).setOnClickListener { sendChat() }
        chatInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                sendChat()
                true
            } else false
        }

        seekPlay.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar, value: Int, fromUser: Boolean) {
                labelPlay.text = "$value%"
                client?.audio?.setPlayVolume(value / 100f)
            }

            override fun onStartTrackingTouch(sb: SeekBar) {}
            override fun onStopTrackingTouch(sb: SeekBar) {
                prefs.edit().putInt("playVol", sb.progress).apply()
            }
        })
        seekMic.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar, value: Int, fromUser: Boolean) {
                labelMic.text = "$value%"
                client?.audio?.setMicGain(value / 100f)
            }

            override fun onStartTrackingTouch(sb: SeekBar) {}
            override fun onStopTrackingTouch(sb: SeekBar) {
                prefs.edit().putInt("micVol", sb.progress).apply()
            }
        })

        findViewById<Button>(R.id.btnPrev).setOnClickListener {
            tracks.prev()
            onTrackChanged()
        }
        findViewById<Button>(R.id.btnNext).setOnClickListener {
            tracks.next()
            onTrackChanged()
        }
        btnPlay.setOnClickListener {
            tracks.toggle()
            updatePlayLabel()
            handler.postDelayed({ tick() }, 300)
        }
        findViewById<Button>(R.id.btnBack).setOnClickListener {
            tracks.seekBy(-10_000)
            tick()
        }
        findViewById<Button>(R.id.btnForward).setOnClickListener {
            tracks.seekBy(10_000)
            tick()
        }
        findViewById<Button>(R.id.btnRescan).setOnClickListener { rescan() }

        seekTrack.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar, value: Int, fromUser: Boolean) {
                if (!fromUser || !scrubbing) return
                val dur = tracks.duration()
                if (dur > 0) tracks.seekTo(dur * value / 1000)
            }

            override fun onStartTrackingTouch(sb: SeekBar) {
                scrubbing = true
            }

            override fun onStopTrackingTouch(sb: SeekBar) {
                scrubbing = false
            }
        })

        askPermissions()
        handler.postDelayed({ tick() }, 500)
        refreshTrackUi()
    }

    override fun onDestroy() {
        super.onDestroy()
        handler.removeCallbacksAndMessages(null)
        client?.disconnect()
        tracks.release()
    }

    // -- вкладки -------------------------------------------------------------

    private fun showPage(position: Int) {
        playerPageActive = position == 3
        for (i in pages.indices) {
            pages[i].visibility = if (i == position) View.VISIBLE else View.GONE
        }
    }

    // -- подключение ---------------------------------------------------------

    private fun askPermissions() {
        val needed = ArrayList<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            needed.add(Manifest.permission.RECORD_AUDIO)
        }
        val media = if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_AUDIO
        else Manifest.permission.READ_EXTERNAL_STORAGE
        if (ContextCompat.checkSelfPermission(this, media)
            != PackageManager.PERMISSION_GRANTED) {
            needed.add(media)
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), REQ_PERMS)
        }
    }

    private fun toggleConnect() {
        val current = client
        if (current != null) {
            current.disconnect()
            client = null
            statusText.text = "Не подключено"
            btnConnect.text = "Подключиться"
            connectPanel.visibility = View.VISIBLE
            return
        }
        val host = editHost.text.toString().trim()
        val name = editName.text.toString().trim()
        if (host.isEmpty()) {
            statusText.text = "Введите адрес преподавателя."
            editHost.requestFocus()
            return
        }
        val shownName = if (name.isEmpty()) "Ученик" else name
        getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("host", host)
            .putString("name", name)
            .apply()

        askPermissions()
        statusText.text = "Подключаемся к $host…"
        val c = NetClient(
            handler, shownName,
            { text -> statusText.text = text },
            { from, text -> addChat(from, text) },
            { list -> mergeHistory(list) },
            { teacher ->
                statusText.text = "В кабинете: $teacher"
                btnConnect.text = "Отключиться"
                connectPanel.visibility = View.GONE
                applyStoredVolumes()
            },
            { reason -> onEnded(reason) },
            { on ->
                if (on) videoHint.text = "Видео включено"
                else videoHint.text = "Видео преподавателя выключено"
            },
            { bmp -> showFrame(bmp) })
        client = c
        c.connect(host)
    }

    private fun onEnded(reason: String) {
        val text = when (reason) {
            "rejected" -> "Преподаватель не пустил в кабинет."
            "closed" -> "Урок закончен."
            "replaced" -> "Вас сменило другое подключение."
            "lost" -> "Связь с преподавателем потеряна."
            else -> "Связь с преподавателем потеряна."
        }
        statusText.text = text
        btnConnect.text = "Подключиться"
        connectPanel.visibility = View.VISIBLE
        videoHint.text = "Видео преподавателя появится здесь"
        videoView.setImageDrawable(null)
        fullVideo.setImageDrawable(null)
        client = null
    }

    private fun applyStoredVolumes() {
        client?.audio?.setPlayVolume(seekPlay.progress / 100f)
        client?.audio?.setMicGain(seekMic.progress / 100f)
    }

    // -- видео ---------------------------------------------------------------

    private fun showFrame(bmp: Bitmap) {
        videoHint.visibility = View.GONE
        videoView.setImageBitmap(bmp)
        if (fullVideoBox.visibility == View.VISIBLE) fullVideo.setImageBitmap(bmp)
    }

    private fun expandVideo(on: Boolean) {
        fullVideoBox.visibility = if (on) View.VISIBLE else View.GONE
        if (on) {
            videoView.drawable?.let { fullVideo.setImageDrawable(it) }
        }
        findViewById<Button>(R.id.btnExpand).text = "Развернуть видео"
    }

    // -- микрофон ------------------------------------------------------------

    private fun toggleMic() {
        val engine = client?.audio
        if (engine == null) {
            statusText.text = "Сначала подключитесь к преподавателю."
            return
        }
        engine.micOn = !engine.micOn
        btnMic.text = if (engine.micOn) "Микрофон включён" else "Микрофон выключен"
    }

    // -- чат -----------------------------------------------------------------

    private fun addChat(from: String, text: String) {
        chatItems.add("$from: $text")
        chatAdapter.notifyDataSetChanged()
        chatList.setSelection(chatItems.size - 1)
    }

    private fun mergeHistory(list: List<Triple<String, String, String>>) {
        if (list.isEmpty()) return
        chatItems.clear()
        for (item in list) chatItems.add("${item.first}: ${item.second}")
        chatAdapter.notifyDataSetChanged()
        chatList.setSelection(chatItems.size - 1)
    }

    private fun sendChat() {
        val text = chatInput.text.toString().trim()
        if (text.isEmpty()) return
        val c = client
        if (c == null) {
            statusText.text = "Сначала подключитесь к преподавателю."
            return
        }
        if (c.sendChat(text)) chatInput.setText("")
    }

    // -- плеер ---------------------------------------------------------------

    private fun refreshTrackUi() {
        if (!tracks.hasTracks()) {
            trackName.text = "Трек не выбран"
            trackStatus.text = "Треки не найдены. Положите файлы в папку «Music/Мажорный лад» и нажмите «Обновить список треков»."
            btnPlay.text = "Играть"
            return
        }
        trackName.text = tracks.currentName()
        trackStatus.text = "Трек ${tracks.index + 1} из ${tracks.tracks.size}"
        updatePlayLabel()
    }

    private fun updatePlayLabel() {
        btnPlay.text = if (tracks.playing) "Стоп" else "Играть"
    }

    private fun onTrackChanged() {
        refreshTrackUi()
        handler.postDelayed({ tick() }, 300)
    }

    private fun rescan() {
        val count = tracks.scan()
        refreshTrackUi()
        trackStatus.text = if (count == 0) trackStatus.text
        else "Найдено треков: $count. Трек ${tracks.index + 1} из $count"
    }

    private fun tick() {
        if (!playerPageActive) {
            handler.postDelayed({ tick() }, 700)
            return
        }
        val dur = tracks.duration()
        if (dur > 0) {
            seekTrack.progress = (tracks.position() * 1000 / dur).coerceIn(0, 1000)
        }
        updatePlayLabel()
        handler.postDelayed({ tick() }, 500)
    }

    // -- аппаратные клавиши --------------------------------------------------

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (!playerPageActive || event.action != KeyEvent.ACTION_DOWN) {
            return super.dispatchKeyEvent(event)
        }
        if (fullVideoBox.visibility == View.VISIBLE) {
            if (event.keyCode == KeyEvent.KEYCODE_BACK) {
                expandVideo(false)
                return true
            }
        }
        when (event.keyCode) {
            KeyEvent.KEYCODE_DPAD_UP -> {
                tracks.next()
                onTrackChanged()
                return true
            }
            KeyEvent.KEYCODE_DPAD_DOWN -> {
                tracks.prev()
                onTrackChanged()
                return true
            }
            KeyEvent.KEYCODE_DPAD_LEFT -> {
                tracks.seekBy(-10_000)
                tick()
                return true
            }
            KeyEvent.KEYCODE_DPAD_RIGHT -> {
                tracks.seekBy(10_000)
                tick()
                return true
            }
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER,
            KeyEvent.KEYCODE_SPACE, KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> {
                tracks.toggle()
                updatePlayLabel()
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onBackPressed() {
        if (fullVideoBox.visibility == View.VISIBLE) {
            expandVideo(false)
            return
        }
        super.onBackPressed()
    }
}
