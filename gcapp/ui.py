"""Интерфейс программы «Мажорный лад».

Всё рассчитано на работу с экранным доступом: у каждого элемента есть
доступное имя и описание, порядок обхода задан явно, изменения состояния
объявляются через QAccessible — NVDA и JAWS проговаривают их сами,
без наведения мыши.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAccessible, QAccessibleEvent, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSlider, QSpinBox, QSplitter,
    QStackedWidget, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from . import __version__, audio, config, updater
from .net import Client, Server
from .video import FrameReassembler, VideoSender

VOLUME_KEYS = [
    ("student", "Звук ученика", "Громкость звука, приходящего от собеседника."),
    ("in1", "Аудиоустройство 1", "Громкость первого аудиовхода."),
    ("in2", "Аудиоустройство 2", "Громкость второго аудиовхода."),
    ("usb", "USB-устройство", "Громкость звука с USB-устройства, например с камеры."),
    ("midi", "MIDI-устройство", "Громкость MIDI-клавиатуры."),
    ("metronome", "Громкость метронома",
     "Громкость щелчков метронома. Появляется, когда метроном включён."),
]


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 1 удар, 2 удара, 5 ударов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def announce(widget: QWidget, text: str) -> None:
    """Объявляет изменение экранному диктору. Ошибки не критичны."""
    widget.setAccessibleDescription(text)
    try:
        QAccessible.updateAccessibility(
            QAccessibleEvent(widget, QAccessible.Event.Alert)
        )
    except Exception:
        pass


def _explain(exc: Exception) -> str:
    """Переводит сетевую ошибку на понятный язык."""
    text = str(exc)
    lowered = text.lower()
    if "urlopen error" in lowered or "timed out" in lowered or "getaddrinfo" in lowered:
        return "нет связи с сервером обновлений. Проверьте интернет и попробуйте ещё раз."
    if "403" in text:
        return "сервер обновлений слишком часто спрашивали. Попробуйте позже."
    if "404" in text:
        return "на сервере обновлений нет подходящего файла."
    return text or exc.__class__.__name__


def label_for(text: str, buddy: QWidget | None = None, description: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setAccessibleName(text)
    if description:
        lbl.setAccessibleDescription(description)
    if buddy is not None:
        lbl.setBuddy(buddy)
    return lbl


class SettingsDialog(QDialog):
    """Настройки. Открывается по Control+P, дерево на четыре раздела."""

    sig_update = Signal(dict)
    sig_progress = Signal(int)
    sig_download = Signal(dict)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._downloading = False
        self.sig_update.connect(self._on_update_result)
        self.sig_progress.connect(self._on_progress)
        self.sig_download.connect(self._on_download_done)
        self.setWindowTitle("Настройки")
        self.setAccessibleName("Настройки «Мажорный лад»")
        self.resize(720, 520)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName("Разделы настроек")
        self.tree.setAccessibleDescription(
            "Дерево из четырёх разделов. Стрелки вверх и вниз переключают раздел."
        )
        self.stack = QStackedWidget()
        self.stack.setAccessibleName("Содержимое раздела")

        sections = [
            ("Общее", self._page_general()),
            ("Аудиовходы", self._page_inputs()),
            ("Аудиовыходы", self._page_outputs()),
            ("Видео", self._page_video()),
        ]
        for title, page in sections:
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.AccessibleTextRole, title)
            self.tree.addTopLevelItem(item)
            self.stack.addWidget(page)
        self.tree.currentItemChanged.connect(self._on_section)
        self.tree.setCurrentItem(self.tree.topLevelItem(0))

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(self.stack)
        split.setSizes([220, 500])

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.button(QDialogButtonBox.Save).setAccessibleName("Сохранить настройки")
        buttons.button(QDialogButtonBox.Cancel).setAccessibleName("Отменить изменения")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addWidget(buttons)

    def _on_section(self, current, previous) -> None:
        if current is None:
            return
        index = self.tree.indexOfTopLevelItem(current)
        self.stack.setCurrentIndex(index)
        announce(self.stack.currentWidget(), f"Раздел {current.text(0)}")

    # -- страницы -----------------------------------------------------------

    def _page_general(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Общее")
        form = QFormLayout(page)

        self.role_label = QLabel("Преподаватель" if self.cfg.get("role") == "teacher" else "Ученик")
        self.role_label.setAccessibleName("Роль в программе")
        form.addRow(label_for("Роль"), self.role_label)

        self.name_edit = QLineEdit(self.cfg.get("name", ""))
        self.name_edit.setAccessibleName("Ваше имя")
        self.name_edit.setAccessibleDescription(
            "Это имя видит собеседник. Ученик может изменить его здесь в любой момент."
        )
        self.name_edit.setPlaceholderText("Ваше имя")
        form.addRow(label_for("Имя", self.name_edit), self.name_edit)

        self.ip_edit = QLineEdit(self.cfg.get("server_ip", ""))
        self.ip_edit.setAccessibleName("Адрес преподавателя")
        self.ip_edit.setAccessibleDescription(
            "IP-адрес компьютера преподавателя. Заполняется учеником один раз."
        )
        self.ip_edit.setPlaceholderText("например 192.168.1.100")
        form.addRow(label_for("Адрес преподавателя", self.ip_edit), self.ip_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(self.cfg.get("server_port", 45800)))
        self.port_spin.setAccessibleName("Порт соединения")
        form.addRow(label_for("Порт", self.port_spin), self.port_spin)

        self.autoconnect_box = QCheckBox("Подключаться автоматически при запуске")
        self.autoconnect_box.setChecked(bool(self.cfg.get("autoconnect", True)))
        self.autoconnect_box.setAccessibleName("Автоматическое подключение при запуске")
        form.addRow(self.autoconnect_box)

        version_row = QHBoxLayout()
        self.version_label = QLabel(f"Версия {__version__}")
        self.version_label.setAccessibleName(f"Версия программы {__version__}")
        self.update_button = QPushButton("Проверить обновления")
        self.update_button.setAccessibleName("Проверить обновления")
        self.update_button.setAccessibleDescription(
            "Проверяет, не вышла ли новая версия программы. "
            "Если вышла, её можно скачать и установить прямо здесь."
        )
        self.update_button.clicked.connect(self._check_updates)
        version_row.addWidget(self.version_label, 1)
        version_row.addWidget(self.update_button)
        form.addRow(version_row)

        self.update_status = QLabel("")
        self.update_status.setWordWrap(True)
        self.update_status.setAccessibleName("Состояние обновления")
        form.addRow(self.update_status)

        return page

    def _device_combo(self, current: str, name: str, description: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(False)
        combo.setAccessibleName(name)
        combo.setAccessibleDescription(description)
        combo.addItem("Не выбрано", "")
        for dev in audio.list_input_devices():
            combo.addItem(dev, dev)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo

    def _page_inputs(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Аудиовходы")
        layout = QVBoxLayout(page)

        tabs = QTabWidget()
        tabs.setAccessibleName("Вкладки аудиовходов")
        tabs.setAccessibleDescription(
            "Четыре вкладки: аудиоустройство 1, аудиоустройство 2, MIDI-устройства, USB-устройство. "
            "Переключаются Control+Tab."
        )

        self.dev1_combo = self._device_combo(
            self.cfg["audio"].get("device1", ""), "Аудиоустройство 1",
            "Основная звуковая карта. Для гитары или микрофона.")
        tabs.addTab(self._device_tab(self.dev1_combo), "Аудиоустройство 1")

        self.dev2_combo = self._device_combo(
            self.cfg["audio"].get("device2", ""), "Аудиоустройство 2",
            "Вторая звуковая карта или второй вход первой карты.")
        tabs.addTab(self._device_tab(self.dev2_combo), "Аудиоустройство 2")

        midi_page = QWidget()
        midi_form = QFormLayout(midi_page)
        self.midi_box = QCheckBox("Использовать MIDI-клавиатуру")
        self.midi_box.setChecked(bool(self.cfg["audio"].get("midi_enabled", False)))
        self.midi_box.setAccessibleName("Использовать MIDI-клавиатуру")
        self.midi_box.setAccessibleDescription(
            "Включает встроенный синтезатор: нажатые ноты слышит и ученик."
        )
        midi_form.addRow(self.midi_box)

        self.midi_combo = QComboBox()
        self.midi_combo.setAccessibleName("MIDI-устройство")
        self.midi_combo.addItem("Первое доступное", "")
        for port in _midi_ports():
            self.midi_combo.addItem(port, port)
        index = self.midi_combo.findData(self.cfg["audio"].get("midi_port", ""))
        self.midi_combo.setCurrentIndex(index if index >= 0 else 0)
        midi_form.addRow(label_for("Порт", self.midi_combo), self.midi_combo)

        self.midi_fixed_box = QCheckBox("Всегда полная громкость нот")
        self.midi_fixed_box.setChecked(
            bool(self.cfg["audio"].get("midi_fixed_velocity", False)))
        self.midi_fixed_box.setAccessibleName("Всегда полная громкость нот")
        self.midi_fixed_box.setAccessibleDescription(
            "Если включено, сила нажатия клавиши не учитывается: каждая нота "
            "звучит одинаково громко, на полной velocity."
        )
        midi_form.addRow(self.midi_fixed_box)
        tabs.addTab(midi_page, "MIDI-устройства")

        self.usb_combo = self._device_combo(
            self.cfg["audio"].get("usb", ""), "USB-устройство",
            "Звук с USB-камеры или другого USB-устройства.")
        tabs.addTab(self._device_tab(self.usb_combo), "USB-устройство")

        layout.addWidget(tabs)
        hint = QLabel(
            "Список устройств обновляется при открытии настроек. "
            "Новую звуковую карту лучше подключить до запуска программы."
        )
        hint.setWordWrap(True)
        hint.setAccessibleName("Подсказка")
        layout.addWidget(hint)
        return page

    def _device_tab(self, combo: QComboBox) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        combo_label = label_for("Устройство", combo,
                                "Стрелки вверх и вниз перебирают устройства.")
        form.addRow(combo_label, combo)
        return page

    def _page_outputs(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Аудиовыходы")
        form = QFormLayout(page)
        self.out_combo = QComboBox()
        self.out_combo.setAccessibleName("Устройство вывода")
        self.out_combo.setAccessibleDescription(
            "Куда выводится звук: наушники, колонки, звуковая карта."
        )
        self.out_combo.addItem("Системное по умолчанию", "")
        for dev in audio.list_output_devices():
            self.out_combo.addItem(dev, dev)
        index = self.out_combo.findData(self.cfg["audio"].get("output", ""))
        self.out_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow(label_for("Устройство вывода", self.out_combo), self.out_combo)

        hint = QLabel(
            "Для занятий нужны наушники: без них микрофон поймает колонки "
            "и вернёт эхо собеседнику."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _page_video(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Видео")
        form = QFormLayout(page)

        self.video_box = QCheckBox("Включить видео")
        self.video_box.setChecked(bool(self.cfg["video"].get("enabled", False)))
        self.video_box.setAccessibleName("Включить видео")
        form.addRow(self.video_box)

        self.cam_spin = QSpinBox()
        self.cam_spin.setRange(0, 9)
        self.cam_spin.setValue(int(self.cfg["video"].get("camera", 0)))
        self.cam_spin.setAccessibleName("Номер камеры")
        self.cam_spin.setAccessibleDescription("Ноль — первая камера в системе.")
        form.addRow(label_for("Номер камеры", self.cam_spin), self.cam_spin)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(int(self.cfg["video"].get("fps", 15)))
        self.fps_spin.setAccessibleName("Кадров в секунду")
        form.addRow(label_for("Кадров в секунду", self.fps_spin), self.fps_spin)

        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(20, 95)
        self.quality_spin.setValue(int(self.cfg["video"].get("quality", 60)))
        self.quality_spin.setAccessibleName("Качество видео")
        self.quality_spin.setAccessibleDescription(
            "Чем выше, тем чётче картинка, но тем больше нагрузка на канал."
        )
        form.addRow(label_for("Качество", self.quality_spin), self.quality_spin)

        self.show_remote_box = QCheckBox("Показывать видео собеседника")
        self.show_remote_box.setChecked(bool(self.cfg["video"].get("show_remote", True)))
        self.show_remote_box.setAccessibleName("Показывать видео собеседника")
        form.addRow(self.show_remote_box)

        return page

    # -- обновления ---------------------------------------------------------

    def _check_updates(self) -> None:
        self.update_button.setEnabled(False)
        self.update_button.setText("Проверяю…")
        self._set_update_status("Проверяю обновления, подождите.")
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self) -> None:
        try:
            info = updater.latest_release(self.cfg.get("role", "teacher"), __version__)
        except Exception as exc:
            self.sig_update.emit({"error": _explain(exc)})
            return
        self.sig_update.emit(info)

    def _on_update_result(self, info: dict) -> None:
        self.update_button.setEnabled(True)
        self.update_button.setText("Проверить обновления")
        if info.get("error"):
            self._set_update_status(f"Проверить обновления не удалось: {info['error']}")
            return
        if info.get("none"):
            self._set_update_status(f"У вас последняя версия, {__version__}.")
            return
        version = info.get("version", "")
        answer = QMessageBox.question(
            self, "Доступно обновление",
            f"Вышла версия {version}. Скачать и установить сейчас?\n\n"
            "Программа закроется, заменит свой файл и запустится заново. "
            "Настройки и имя сохранятся.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            self._set_update_status(f"Доступна версия {version}. Скачать можно позже.")
            return
        self._start_download(info.get("url", ""))

    def _start_download(self, url: str) -> None:
        self._downloading = True
        self.update_button.setEnabled(False)
        self.update_button.setText("Скачиваю…")
        self._set_update_status(
            "Скачиваю обновление. Это займёт несколько минут, окно закроется само."
        )
        threading.Thread(target=self._download_worker, args=(url,), daemon=True).start()

    def _download_worker(self, url: str) -> None:
        try:
            path = updater.download(url, progress=self.sig_progress.emit)
        except Exception as exc:
            self.sig_download.emit({"error": _explain(exc)})
            return
        self.sig_download.emit({"path": str(path)})

    def _on_progress(self, percent: int) -> None:
        self.update_status.setText(f"Скачиваю обновление: {percent} процентов.")

    def _on_download_done(self, info: dict) -> None:
        self._downloading = False
        if info.get("error"):
            self.update_button.setEnabled(True)
            self.update_button.setText("Проверить обновления")
            self._set_update_status(f"Скачать обновление не удалось: {info['error']}")
            return
        self._set_update_status("Обновление скачано. Перезапускаю программу.")
        ok, error = updater.install_and_restart(Path(info.get("path", "")))
        if not ok:
            self.update_button.setEnabled(True)
            self.update_button.setText("Проверить обновления")
            self._set_update_status(error)
            return
        config.save(self.cfg)
        app = QApplication.instance()
        self.reject()
        if app is not None:
            QTimer.singleShot(300, app.quit)

    def _set_update_status(self, text: str) -> None:
        self.update_status.setText(text)
        announce(self.update_status, text)

    # -- сохранение ---------------------------------------------------------

    def _apply(self) -> None:
        if self._downloading:
            QMessageBox.information(
                self, "Обновление",
                "Скачивание обновления ещё идёт. Подождите: программа закроется сама."
            )
            return
        cfg = self.cfg
        new_name = self.name_edit.text().strip()
        if new_name:
            cfg["name"] = new_name
        cfg["server_ip"] = self.ip_edit.text().strip()
        cfg["server_port"] = int(self.port_spin.value())
        cfg["autoconnect"] = self.autoconnect_box.isChecked()

        cfg["audio"]["device1"] = self.dev1_combo.currentData() or ""
        cfg["audio"]["device2"] = self.dev2_combo.currentData() or ""
        cfg["audio"]["usb"] = self.usb_combo.currentData() or ""
        cfg["audio"]["output"] = self.out_combo.currentData() or ""
        cfg["audio"]["midi_enabled"] = self.midi_box.isChecked()
        cfg["audio"]["midi_port"] = self.midi_combo.currentData() or ""
        cfg["audio"]["midi_fixed_velocity"] = self.midi_fixed_box.isChecked()

        cfg["video"]["enabled"] = self.video_box.isChecked()
        cfg["video"]["camera"] = int(self.cam_spin.value())
        cfg["video"]["fps"] = int(self.fps_spin.value())
        cfg["video"]["quality"] = int(self.quality_spin.value())
        cfg["video"]["show_remote"] = self.show_remote_box.isChecked()

        config.save(cfg)
        self.accept()


def _midi_ports() -> list[str]:
    try:
        from .midi_synth import list_midi_ports
        return list_midi_ports()
    except Exception:
        return []


class MainWindow(QMainWindow):
    sig_status = Signal(str)
    sig_chat = Signal(str, str)
    sig_peer = Signal(bool)
    sig_ready = Signal(str)
    sig_busy = Signal()
    sig_video = Signal(object)
    sig_join_request = Signal(object)
    sig_ended = Signal(str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.is_teacher = cfg.get("role") == "teacher"
        self.engine = audio.AudioEngine(log=self._log_threadsafe)
        self.reassembler = FrameReassembler(on_frame=self.sig_video.emit,
                                            log=self._log_threadsafe)
        self.engine.on_video = self.reassembler.feed
        self.server: Server | None = None
        self.client: Client | None = None
        self._join_queue: list = []
        self._join_dialog_open = False
        self.video_sender: VideoSender | None = None
        self.connected = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(lambda: config.save(self.cfg))

        self.setWindowTitle(f"Мажорный лад {__version__}")
        self.resize(900, 680)
        self.setAccessibleName("Мажорный лад, главное окно")

        self._build_ui()
        self._wire_signals()
        self._apply_volumes()
        self._apply_metronome()

        shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        shortcut.activated.connect(self.open_settings)

        if self.is_teacher:
            self._start_teacher()
        else:
            QTimer.singleShot(300, self._start_student)

    # -- построение интерфейса ----------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        central.setAccessibleName("Главное окно")
        layout = QVBoxLayout(central)

        self.status_label = QLabel("Готово.")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Состояние")
        layout.addWidget(self.status_label)

        # Видео собеседника
        self.video_label = QLabel()
        self.video_label.setMinimumHeight(240)
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setAccessibleName("Видео собеседника")
        self.video_label.setVisible(bool(self.cfg["video"].get("enabled")
                                         and self.cfg["video"].get("show_remote", True)))
        layout.addWidget(self.video_label, 1)

        # Громкости
        vol_group = QGroupBox("Громкость")
        vol_group.setAccessibleName("Регуляторы громкости")
        vol_layout = QVBoxLayout(vol_group)
        self.sliders: dict[str, QSlider] = {}
        self.slider_values: dict[str, QLabel] = {}
        self.slider_rows: dict[str, QWidget] = {}
        self.slider_titles: dict[str, str] = {}
        for key, title, description in VOLUME_KEYS:
            if key == "student":
                title = "Звук ученика" if self.is_teacher else "Звук преподавателя"
            self.slider_titles[key] = title
            row_widget = QWidget()
            row_widget.setAccessibleName(title)
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            name_label = QLabel(title)
            name_label.setMinimumWidth(180)
            name_label.setAccessibleName(title)
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(int(self.cfg["audio"]["volumes"].get(key, 100)))
            slider.setTickPosition(QSlider.TicksBelow)
            slider.setTickInterval(10)
            slider.setAccessibleName(title)
            slider.setAccessibleDescription(
                description + " Стрелки влево и вправо меняют громкость, Home и End — до предела."
            )
            name_label.setBuddy(slider)
            value_label = QLabel(f"{slider.value()} процентов")
            value_label.setMinimumWidth(110)
            value_label.setAccessibleName(f"{title}, значение")
            row.addWidget(name_label)
            row.addWidget(slider, 1)
            row.addWidget(value_label)
            vol_layout.addWidget(row_widget)
            self.sliders[key] = slider
            self.slider_values[key] = value_label
            self.slider_rows[key] = row_widget
            slider.valueChanged.connect(
                lambda value, k=key, t=title: self._on_volume(k, t, value)
            )
        self._refresh_volume_visibility()
        layout.addWidget(vol_group)

        # Метроном
        metro_group = QGroupBox("Метроном")
        metro_group.setAccessibleName("Метроном")
        metro_layout = QVBoxLayout(metro_group)

        self.metro_button = QPushButton()
        self.metro_button.setCheckable(True)
        metro_layout.addWidget(self.metro_button)

        self.metro_row = QWidget()
        metro_row = QHBoxLayout(self.metro_row)
        metro_row.setContentsMargins(0, 0, 0, 0)
        self.bpm_spin = QSpinBox()
        self.bpm_spin.setRange(30, 300)
        self.bpm_spin.setAccessibleName("Темп, ударов в минуту")
        self.bpm_spin.setAccessibleDescription(
            "Темп метронома. Стрелки вверх и вниз меняют на один удар, "
            "Page Up и Page Down — на десять. Можно набрать число с клавиатуры."
        )
        self.beats_spin = QSpinBox()
        self.beats_spin.setRange(1, 12)
        self.beats_spin.setAccessibleName("Ритм, долей в такте")
        self.beats_spin.setAccessibleDescription(
            "Сколько долей в такте, например 4. Первая доля звучит выше и громче."
        )
        metro_row.addWidget(label_for("Темп, ударов в минуту", self.bpm_spin))
        metro_row.addWidget(self.bpm_spin)
        metro_row.addSpacing(16)
        metro_row.addWidget(label_for("Ритм, долей в такте", self.beats_spin))
        metro_row.addWidget(self.beats_spin)
        metro_row.addStretch(1)
        metro_layout.addWidget(self.metro_row)
        layout.addWidget(metro_group)

        # Имя и подключение (для ученика)
        self.name_row = QWidget()
        name_layout = QHBoxLayout(self.name_row)
        name_layout.setContentsMargins(0, 0, 0, 0)
        self.name_edit = QLineEdit(self.cfg.get("name", ""))
        self.name_edit.setAccessibleName("Ваше имя")
        self.name_edit.setAccessibleDescription("Введите имя и нажмите Присоединиться.")
        self.name_edit.setPlaceholderText("Ваше имя")
        self.join_button = QPushButton("Присоединиться")
        self.join_button.setAccessibleName("Присоединиться к преподавателю")
        name_layout.addWidget(label_for("Имя", self.name_edit))
        name_layout.addWidget(self.name_edit, 1)
        name_layout.addWidget(self.join_button)
        self.name_row.setVisible(not self.is_teacher)
        layout.addWidget(self.name_row)

        # Чат
        chat_group = QGroupBox("Чат")
        chat_group.setAccessibleName("Чат")
        chat_layout = QVBoxLayout(chat_group)
        self.chat_view = QPlainTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setAccessibleName("История чата")
        self.chat_view.setAccessibleDescription("История сообщений и файлов. Стрелки прокручивают.")
        chat_layout.addWidget(self.chat_view, 1)

        input_row = QHBoxLayout()
        self.message_edit = QLineEdit()
        self.message_edit.setAccessibleName("Сообщение")
        self.message_edit.setAccessibleDescription("Введите сообщение и нажмите Enter.")
        self.send_button = QPushButton("Отправить")
        self.send_button.setAccessibleName("Отправить сообщение")
        self.file_button = QPushButton("Отправить файл")
        self.file_button.setAccessibleName("Отправить файл собеседнику")
        input_row.addWidget(label_for("Сообщение", self.message_edit))
        input_row.addWidget(self.message_edit, 1)
        input_row.addWidget(self.send_button)
        input_row.addWidget(self.file_button)
        chat_layout.addLayout(input_row)
        layout.addWidget(chat_group, 2)

        # Кнопки внизу
        bottom = QHBoxLayout()
        self.settings_button = QPushButton("Настройки (Control+P)")
        self.settings_button.setAccessibleName("Настройки, Control P")
        self.connect_button = QPushButton("Открыть кабинет" if self.is_teacher else "Подключиться")
        self.connect_button.setAccessibleName(self.connect_button.text())
        bottom.addWidget(self.settings_button)
        bottom.addWidget(self.connect_button)
        bottom.addStretch(1)
        layout.addLayout(bottom)

        self.setCentralWidget(central)
        self.message_edit.returnPressed.connect(self._send_message)
        self.send_button.clicked.connect(self._send_message)
        self.file_button.clicked.connect(self._send_file)
        self.settings_button.clicked.connect(self.open_settings)
        self.join_button.clicked.connect(self._join)
        self.connect_button.clicked.connect(self._on_connect_button)
        self.metro_button.toggled.connect(self._on_metronome_toggle)
        self.bpm_spin.valueChanged.connect(self._on_metronome_value)
        self.beats_spin.valueChanged.connect(self._on_metronome_value)

    def _wire_signals(self) -> None:
        self.sig_status.connect(self._set_status)
        self.sig_chat.connect(self._append_chat)
        self.sig_peer.connect(self._on_peer)
        self.sig_ready.connect(self._on_ready)
        self.sig_busy.connect(self._on_busy)
        self.sig_video.connect(self._on_video_frame)
        self.sig_join_request.connect(self._on_join_request)
        self.sig_ended.connect(self._on_ended)

    # -- запуск ролей -------------------------------------------------------

    def _start_teacher(self) -> None:
        name = self.cfg.get("name") or "Преподаватель"
        self.server = Server(
            name,
            on_status=self.sig_status.emit,
            on_chat=self.sig_chat.emit,
            on_file=lambda who, path: self.sig_chat.emit(
                "система", f"{who} прислал файл: {os.path.basename(path)}"),
            on_peer=self.sig_peer.emit,
            on_join_request=self.sig_join_request.emit,
            port=int(self.cfg.get("server_port", 45800)),
        )
        if self.server.start():
            self.connect_button.setText("Закрыть кабинет")
            self.connect_button.setAccessibleName("Закрыть кабинет")
            self._append_chat("система", "Кабинет открыт. Жду ученика.")
            # Звуковой поток открываем сразу, не дожидаясь ученика: иначе
            # метроном и MIDI-клавиатура молчат, пока никто не подключился.
            if self._start_audio():
                self._set_status("Кабинет открыт, звук запущен. Жду ученика.")

    def _start_student(self) -> None:
        name = self.cfg.get("name", "").strip()
        if not name:
            self._set_status("Введите имя и нажмите «Присоединиться».")
            announce(self.name_edit, "Введите ваше имя")
            self.name_edit.setFocus()
            return
        self.name_edit.setText(name)
        self.name_row.setVisible(False)
        # Свой метроном и своя MIDI-клавиатура должны звучать и до подключения.
        self._start_audio(local_only=True)
        if self.cfg.get("autoconnect", True):
            self._connect()

    def _connect(self) -> None:
        host = self.cfg.get("server_ip", "").strip()
        if not host:
            self._set_status("Не задан адрес преподавателя. Откройте настройки, Control+P.")
            QMessageBox.information(
                self, "Нужен адрес",
                "В настройках, раздел «Общее», укажите адрес преподавателя. "
                "Он выдаёт этот адрес ученикам."
            )
            return
        name = self.cfg.get("name", "").strip() or self.name_edit.text().strip()
        if not name:
            self._set_status("Сначала введите имя.")
            return
        self.client = Client(
            name, host,
            port=int(self.cfg.get("server_port", 45800)),
            on_status=self.sig_status.emit,
            on_chat=self.sig_chat.emit,
            on_file=lambda who, path: self.sig_chat.emit(
                "система", f"{who} прислал файл: {os.path.basename(path)}"),
            on_ready=self.sig_ready.emit,
            on_busy=self.sig_busy.emit,
            on_peer=self.sig_peer.emit,
            on_ended=self.sig_ended.emit,
        )
        self._set_status(f"Подключаюсь к {host}…")
        # connect() может ждать до шести секунд — не держим интерфейс.
        threading.Thread(target=self.client.connect, daemon=True).start()

    # -- обработчики --------------------------------------------------------

    def _join(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            announce(self.name_edit, "Введите имя, поле пустое")
            self._set_status("Введите имя, чтобы присоединиться.")
            return
        self.cfg["name"] = name
        config.save(self.cfg)
        self.name_row.setVisible(False)
        self._connect()

    def _on_connect_button(self) -> None:
        if self.is_teacher:
            if self.server is not None:
                self.server.stop()
                self.server = None
                self.engine.stop()
                self.connected = False
                self.connect_button.setText("Открыть кабинет")
                self._set_status("Кабинет закрыт.")
            else:
                self._start_teacher()
        else:
            if self.client is not None:
                self.client.stop()
                self.client = None
                self.engine.stop()
                self.connected = False
                # После отключения местный звук остаётся: метроном и MIDI
                # нужны и для самостоятельной работы.
                self._start_audio(local_only=True)
                self._set_status("Отключено.")
            else:
                self._connect()

    def _on_ready(self, teacher: str) -> None:
        self._set_status(f"Соединение установлено: {teacher}.")
        self._append_chat("система", f"Соединение установлено. {teacher} на связи.")
        self.name_row.setVisible(False)
        self.connect_button.setText("Отключиться")
        self.connect_button.setAccessibleName("Отключиться")
        self._start_audio()

    def _on_busy(self) -> None:
        # Кабинет занят: программа уже спросила преподавателя. Окно не
        # открываем — ответ может прийти и через минуту, а модальное окно
        # держало бы ученика и мешало ему работать.
        text = "Идёт урок с другим учеником. Спросил преподавателя, жду ответа."
        self._set_status(text)
        self._append_chat("система", text)
        announce(self.status_label, text)

    def _on_join_request(self, session) -> None:
        """Второй ученик просит подключиться — решает преподаватель."""
        if self.server is None:
            return
        self._join_queue.append(session)
        if self._join_dialog_open:
            # Окошко уже открыто: покажем это сразу после текущего.
            return
        self._join_dialog_open = True
        try:
            while self._join_queue and self.server is not None:
                self._ask_join(self._join_queue.pop(0))
        finally:
            self._join_dialog_open = False

    def _ask_join(self, session) -> None:
        name = getattr(session, "name", "") or "Второй ученик"
        current = self.server.active_name
        if current:
            text = (f"{name} просит подключиться. Сейчас в кабинете {current}. "
                    f"Подключить {name}? Тогда урок с {current} закончится, "
                    f"и ему придёт сообщение об окончании.")
        else:
            text = f"{name} просит подключиться. Подключить его?"
        self._append_chat("система", text)
        announce(self.status_label, text)
        box = QMessageBox(self)
        box.setWindowTitle("Второй ученик")
        box.setIcon(QMessageBox.Question)
        box.setText(text)
        box.setAccessibleName("Второй ученик просит подключиться")
        accept = box.addButton("Подключить второго", QMessageBox.AcceptRole)
        cancel = box.addButton("Отменить", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        accepted = box.clickedButton() is accept
        if self.server is not None:
            self.server.decide(session, accepted)

    def _on_ended(self, reason: str) -> None:
        """Ученику сообщаем, почему урок закончился."""
        if self.is_teacher:
            return
        texts = {
            "closed": "Урок завершён: преподаватель закрыл кабинет.",
            "replaced": "Урок завершён: преподаватель подключил другого ученика.",
            "rejected": "Извините, идёт урок с другим учеником. Попробуйте позже.",
            "lost": "Связь с преподавателем прервалась. Проверьте, открыт ли кабинет.",
        }
        text = texts.get(reason, texts["lost"])
        self.client = None
        self.connected = False
        self.connect_button.setText("Подключиться")
        self.connect_button.setAccessibleName("Подключиться")
        self._set_status(text)
        self._append_chat("система", text)
        announce(self.status_label, text)

    def _on_peer(self, present: bool) -> None:
        self.connected = bool(present)
        if not present:
            self._stop_video()
            self.engine.student.clear()
            if self.is_teacher:
                # Кабинет остаётся открытым: местный звук, метроном и MIDI
                # продолжают работать, пока ждём следующего ученика.
                # Если кабинет уже закрыт, это запоздалый сигнал от
                # закрытого соединения — он не должен перебивать «Кабинет
                # закрыт».
                if self.server is not None:
                    self._set_status("Ученик отключился. Кабинет свободен.")
            else:
                self.engine.stop()
                self._start_audio(local_only=True)
            return
        if self.is_teacher:
            self._set_status("Ученик подключился.")
            self._append_chat("система", "Ученик подключился.")
            if self.engine.running:
                self._start_video()
            else:
                self._start_audio()

    def _start_audio(self, local_only: bool = False) -> bool:
        """Открывает звуковой поток.

        local_only — только свои наушники, без отправки собеседнику: так
        метроном и MIDI-клавиатура работают до подключения и без сети.
        """
        cfg_audio = self.cfg["audio"]
        devices = {
            "in1": cfg_audio.get("device1", ""),
            "in2": cfg_audio.get("device2", ""),
            "usb": cfg_audio.get("usb", ""),
        }
        is_server = self.is_teacher
        server_addr = None
        if not is_server and not local_only:
            host = self.cfg.get("server_ip", "").strip()
            if host:
                server_addr = (host, audio.UDP_PORT)
        ok, message = self.engine.start(
            is_server=is_server,
            server_addr=server_addr,
            devices=devices,
            output=cfg_audio.get("output", ""),
            midi_enabled=bool(cfg_audio.get("midi_enabled", False)),
            midi_port=cfg_audio.get("midi_port", ""),
            midi_fixed_velocity=bool(
                cfg_audio.get("midi_fixed_velocity", False)),
        )
        self._set_status(message)
        if ok:
            self._apply_volumes()
            self._apply_metronome()
            if self.connected:
                self._start_video()
        return ok

    def _start_video(self) -> None:
        if self.video_sender is not None:
            return
        if not self.cfg["video"].get("enabled"):
            return
        self.video_label.setVisible(bool(self.cfg["video"].get("show_remote", True)))
        self.video_sender = VideoSender(
            camera=int(self.cfg["video"].get("camera", 0)),
            fps=int(self.cfg["video"].get("fps", 15)),
            quality=int(self.cfg["video"].get("quality", 60)),
            send_fn=self.engine.send_video_packet,
            log=self._log_threadsafe,
        )
        ok, message = self.video_sender.start()
        if not ok:
            self._log_threadsafe(message)

    def _stop_video(self) -> None:
        if self.video_sender is not None:
            self.video_sender.stop()
            self.video_sender = None
        self.video_label.clear()

    # -- громкость ----------------------------------------------------------

    def _on_volume(self, key: str, title: str, value: int) -> None:
        self.engine.set_volume(key, value)
        self.cfg["audio"]["volumes"][key] = int(value)
        self.slider_values[key].setText(f"{value} процентов")
        self.slider_values[key].setAccessibleDescription(f"{title}: {value} процентов")
        self._save_timer.start()

    def _apply_volumes(self) -> None:
        for key, slider in self.sliders.items():
            self.engine.set_volume(key, slider.value())

    # -- метроном -----------------------------------------------------------

    def _update_metro_button(self) -> None:
        """Подпись кнопки, доступное имя и видимость полей темпа и ритма."""
        enabled = self.metro_button.isChecked()
        bpm = self.bpm_spin.value()
        beats = self.beats_spin.value()
        if enabled:
            self.metro_button.setText("Выключить метроном")
            self.metro_button.setAccessibleDescription(
                f"Метроном включён: {bpm} {_plural(bpm, 'удар', 'удара', 'ударов')} "
                f"в минуту, {beats} {_plural(beats, 'доля', 'доли', 'долей')} в такте. "
                "Нажмите, чтобы выключить."
            )
        else:
            self.metro_button.setText("Включить метроном")
            self.metro_button.setAccessibleDescription(
                "Нажмите, чтобы включить метроном. Появятся поля темпа и ритма "
                "и регулятор громкости метронома."
            )
        self.metro_button.setAccessibleName(self.metro_button.text())
        self.metro_row.setVisible(enabled)

    def _apply_metronome(self) -> None:
        """Приводит кнопку и поля к сохранённым настройкам."""
        cfg_metro = self.cfg.get("metronome") or {}
        enabled = bool(cfg_metro.get("enabled"))
        bpm = int(cfg_metro.get("bpm", 90) or 90)
        beats = int(cfg_metro.get("beats", 4) or 4)
        for spin, value in ((self.bpm_spin, bpm), (self.beats_spin, beats)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.metro_button.blockSignals(True)
        self.metro_button.setChecked(enabled)
        self.metro_button.blockSignals(False)
        self._update_metro_button()
        self.engine.set_metronome(enabled, bpm, beats)
        self._refresh_volume_visibility()

    def _on_metronome_toggle(self, checked: bool) -> None:
        cfg_metro = self.cfg.setdefault("metronome", {})
        cfg_metro["enabled"] = bool(checked)
        self._update_metro_button()
        self.engine.set_metronome(checked, self.bpm_spin.value(),
                                  self.beats_spin.value())
        config.save(self.cfg)
        appeared = self._refresh_volume_visibility()
        if checked:
            bpm = self.bpm_spin.value()
            beats = self.beats_spin.value()
            text = (f"Метроном включён: {bpm} "
                    f"{_plural(bpm, 'удар', 'удара', 'ударов')} в минуту, "
                    f"{beats} {_plural(beats, 'доля', 'доли', 'долей')} в такте.")
        else:
            text = "Метроном выключен."
        if "metronome" in appeared:
            text += " Регулятор громкости метронома появился на главном экране."
        self._set_status(text)

    def _on_metronome_value(self) -> None:
        cfg_metro = self.cfg.setdefault("metronome", {})
        cfg_metro["bpm"] = self.bpm_spin.value()
        cfg_metro["beats"] = self.beats_spin.value()
        self._update_metro_button()
        self.engine.set_metronome(self.metro_button.isChecked(),
                                  self.bpm_spin.value(), self.beats_spin.value())
        self._save_timer.start()

    def _refresh_volume_visibility(self) -> list[str]:
        """Оставляет на экране только регуляторы выбранных устройств.

        Громкость собеседника есть всегда. Остальные регуляторы появляются,
        когда устройство выбрано в настройках, и прячутся, когда его убрали.
        Возвращает ключи регуляторов, которые только что появились.
        """
        audio_cfg = self.cfg.get("audio", {})
        active = {
            "student": True,
            "in1": bool(str(audio_cfg.get("device1") or "").strip()),
            "in2": bool(str(audio_cfg.get("device2") or "").strip()),
            "usb": bool(str(audio_cfg.get("usb") or "").strip()),
            "midi": bool(audio_cfg.get("midi_enabled")),
            "metronome": bool(self.cfg.get("metronome", {}).get("enabled")),
        }
        appeared: list[str] = []
        for key, row in self.slider_rows.items():
            visible = active.get(key, True)
            if visible and row.isHidden():
                appeared.append(key)
            row.setVisible(visible)
        return appeared

    # -- чат и файлы --------------------------------------------------------

    def _send_message(self) -> None:
        text = self.message_edit.text().strip()
        if not text:
            return
        sent = False
        if self.is_teacher and self.server is not None:
            sent = self.server.send_chat(text)
        elif self.client is not None:
            sent = self.client.send_chat(text)
        if sent:
            self._append_chat("вы", text)
            self.message_edit.clear()
        else:
            self._set_status("Сообщение не отправлено: нет соединения.")

    def _send_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите файл для отправки")
        if not path:
            return
        sent = False
        if self.is_teacher and self.server is not None:
            sent = self.server.send_file(path)
        elif self.client is not None:
            sent = self.client.send_file(path)
        if sent:
            self._append_chat("система", f"Файл отправлен: {Path(path).name}")
        else:
            self._set_status("Файл не отправлен: нет соединения.")

    def _append_chat(self, sender: str, text: str) -> None:
        self.chat_view.appendPlainText(f"{sender}: {text}")
        announce(self.chat_view, f"Новое сообщение от {sender}: {text}")

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        announce(self.status_label, text)

    def _log_threadsafe(self, text: str) -> None:
        self.sig_status.emit(text)

    # -- видео --------------------------------------------------------------

    def _on_video_frame(self, image) -> None:
        if not self.cfg["video"].get("show_remote", True):
            return
        self.video_label.setVisible(True)
        pixmap = QPixmap.fromImage(image)
        self.video_label.setPixmap(
            pixmap.scaled(self.video_label.size(), Qt.KeepAspectRatio,
                          Qt.SmoothTransformation)
        )

    # -- настройки ----------------------------------------------------------

    def open_settings(self) -> None:
        audio_before = dict(self.cfg["audio"])
        dialog = SettingsDialog(self.cfg, self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._apply_volumes()
        # Смена аудиоустройств применяется сразу: поток переоткрывается сам.
        device_keys = ("device1", "device2", "usb", "output")
        devices_changed = any(audio_before.get(key) != self.cfg["audio"].get(key)
                              for key in device_keys)
        if self.engine.running and devices_changed:
            self._start_audio(local_only=not self.connected)
        elif self.engine.running:
            # MIDI применяется отдельно: клавиатура начинает играть без
            # перезапуска кабинета.
            self.engine.set_midi(
                bool(self.cfg["audio"].get("midi_enabled")),
                self.cfg["audio"].get("midi_port", ""),
                bool(self.cfg["audio"].get("midi_fixed_velocity", False)))
        appeared = self._refresh_volume_visibility()
        message = "Настройки сохранены."
        if devices_changed and self.engine.running:
            message += " Звук перезапущен с новыми устройствами."
        if appeared:
            names = ", ".join(self.slider_titles[key] for key in appeared)
            message += f" На главном экране появился регулятор: {names}."
        if not self.is_teacher and self.cfg.get("name"):
            self.name_edit.setText(self.cfg["name"])
        self._set_status(message)

    def closeEvent(self, event) -> None:
        config.save(self.cfg)
        self._stop_video()
        self.engine.stop()
        if self.server is not None:
            self.server.stop()
        if self.client is not None:
            self.client.stop()
        super().closeEvent(event)
