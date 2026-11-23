"""Интерфейс программы «Мажорный лад».

Всё рассчитано на работу с экранным доступом: у каждого элемента есть
доступное имя и описание, порядок обхода задан явно, изменения состояния
объявляются через QAccessible — NVDA и JAWS проговаривают их сами,
без наведения мыши.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAccessible, QAccessibleEvent, QBrush, QColor, QDesktopServices, QPixmap,
)

try:  # Qt 6.8 и новее: «объявление» диктору не зависит от того, где фокус
    from PySide6.QtGui import QAccessibleAnnouncementEvent
except ImportError:  # на более старом Qt остаётся событие «тревога»
    QAccessibleAnnouncementEvent = None
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSlider,
    QSpinBox, QSplitter, QStackedWidget, QTabWidget, QTextEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import (__version__, audio, cameras, config, effects, history, media,
               runlog, shortcuts, sounds, students, updater)
from .net import Client, Server
from .video import FrameReassembler, VideoSender

# Эффекты плеера: шаг скорости и её границы, предел сдвига тона.
_SPEED_STEP = 0.1
_SPEED_MIN = 0.5
_SPEED_MAX = 1.5
_PITCH_LIMIT = 6
# Сколько готовых вариантов трека держим в памяти. Четырёхминутный трек
# занимает около 46 МБ, так что хватает на несколько последних настроек.
_EFFECTS_CACHE_BYTES = 256 * 1024 * 1024

# Пункт списка микрофонов, который всегда означает «тот, что Windows выбрала
# по умолчанию»: на другом компьютере имя устройства другое, а этот работает.
DEFAULT_MIC_LABEL = "Системный микрофон (по умолчанию)"

VOLUME_KEYS = [
    ("student", "Звук ученика"),
    ("in1", "Аудиоустройство 1"),
    ("in2", "Аудиоустройство 2"),
    ("usb", "USB-устройство"),
    ("midi", "MIDI-устройство"),
    ("metronome", "Громкость метронома"),
    ("player", "Громкость трека"),
]


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 1 удар, 2 удара, 5 ударов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _clock(seconds: float) -> str:
    """Время трека словами для диктора: 1:05, 12:30."""
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _percent(speed: float) -> str:
    """Скорость трека словами: 90 процентов."""
    return f"{int(round(speed * 100))} процентов"


def _pitch_word(semitones: int) -> str:
    """Сдвиг тона словами: тон выше на 2 полутона."""
    n = abs(int(semitones))
    return (f"тон {'выше' if semitones > 0 else 'ниже'} "
            f"на {n} {_plural(n, 'полутон', 'полутона', 'полутонов')}")


def _duration_word(seconds: float) -> str:
    """Длительность урока словами: 1 час 5 минут 3 секунды."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} {_plural(hours, 'час', 'часа', 'часов')}")
    if minutes:
        parts.append(f"{minutes} {_plural(minutes, 'минута', 'минуты', 'минут')}")
    if secs or not parts:
        parts.append(f"{secs} {_plural(secs, 'секунда', 'секунды', 'секунд')}")
    return " ".join(parts)


# Строка, через которую программа говорит с экранным диктором. Одна на
# приложение: создаётся в главном окне и живёт, пока оно открыто.
_announcer: "Announcer | None" = None


class Announcer(QLabel):
    """Невидимая строка для объявлений экранному диктору.

    Нужно, чтобы диктор проговорил текст, даже когда фокус стоит не в чате,
    а, скажем, на регуляторе громкости. Qt 6.8 и новее умеет объявления
    (Announcement) — диктор читает их, где бы ни был фокус. На более старом
    Qt остаются события «тревога» и «сменилось имя» по доступному имени
    этой строки.
    """

    # Объявление уходит диктору не в момент вызова, а следующей очередью
    # событий. Так Qt не обращается к Windows из середины обработки
    # сообщения: именно такая вложенная связь давала сбой 0x8001010d при
    # запуске программы.
    _queued = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(1, 1)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("Объявления программы")
        # За один проход может прийти несколько объявлений: диктору хватает
        # последнего, остальные он всё равно не успел бы прочитать.
        self._pending: list[tuple[str, bool]] = []
        self._queued.connect(self._collect, Qt.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._flush)

    def say(self, text: str, polite: bool = True) -> None:
        if not text:
            return
        # Доступное имя меняем сразу: диктор может прочитать его в любой
        # момент, а вот событие отправляем очередью.
        self.setAccessibleName(text)
        self.setAccessibleDescription(text)
        self._queued.emit(text, polite)

    def _collect(self, text: str, polite: bool) -> None:
        self._pending.append((text, polite))
        if not self._timer.isActive():
            self._timer.start()

    def _flush(self) -> None:
        if not self._pending:
            return
        # Срочное объявление важнее вежливого, даже если пришло раньше.
        urgent = any(not polite for _, polite in self._pending)
        text = self._pending[-1][0]
        self._pending.clear()
        self._deliver(text, not urgent)

    def _deliver(self, text: str, polite: bool) -> None:
        if QAccessibleAnnouncementEvent is not None:
            if self._announce(text, polite):
                return
        for kind in (QAccessible.Event.NameChanged, QAccessible.Event.Alert):
            try:
                QAccessible.updateAccessibility(QAccessibleEvent(self, kind))
            except Exception:
                pass

    def _announce(self, text: str, polite: bool) -> bool:
        """Объявление через Qt. Если оно не поддержано — False."""
        try:
            event = QAccessibleAnnouncementEvent(self, text)
        except Exception:
            return False
        try:
            level = (QAccessibleAnnouncementEvent.Polite if polite
                     else QAccessibleAnnouncementEvent.Assertive)
            event.setPoliteness(level)
        except Exception:
            pass
        try:
            QAccessible.updateAccessibility(event)
            return True
        except Exception:
            return False


def _alert(widget: QWidget) -> None:
    for kind in (QAccessible.Event.NameChanged, QAccessible.Event.Alert):
        try:
            QAccessible.updateAccessibility(QAccessibleEvent(widget, kind))
        except Exception:
            pass


def announce(widget: QWidget, text: str) -> None:
    """Объявляет изменение экранному диктору. Ошибки не критичны."""
    # Кнопки состояния (микрофон, камера) сначала получают новое доступное
    # имя, и диктор проговаривает его сам по изменению имени у элемента, на
    # котором стоит фокус. Наше объявление говорило бы то же самое второй
    # раз — микрофон так и читался дважды. Поэтому такой текст не повторяем.
    try:
        if (widget.hasFocus()
                and widget.accessibleName().strip() == text.strip()):
            return
    except Exception:
        pass
    widget.setAccessibleDescription(text)
    if _announcer is not None:
        _announcer.say(text)
        return

    # Главная строка объявлений ещё не создана: сообщаем событием самого
    # элемента — тоже следующим проходом очереди, не из середины обработки
    # текущего сообщения Windows.
    def later() -> None:
        try:
            _alert(widget)
        except RuntimeError:  # элемент к этому моменту уже закрыт
            pass

    QTimer.singleShot(0, later)


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


def label_for(text: str, buddy: QWidget | None = None) -> QLabel:
    lbl = QLabel(text)
    lbl.setAccessibleName(text)
    if buddy is not None:
        lbl.setBuddy(buddy)
    return lbl


# Ссылка в сообщении: адрес с http/https или короткая запись с www.
# Концевая пунктуация в адрес не входит — точка в конце фразы остаётся
# частью предложения, а не ссылки.
_LINK_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)


def find_link(text: str) -> str:
    """Первая ссылка из текста сообщения. Пусто — ссылки нет."""
    match = _LINK_RE.search(str(text or ""))
    if match is None:
        return ""
    link = match.group(0).rstrip(".,;:!?)]}»\"'")
    if link.lower().startswith("www."):
        link = "https://" + link
    return link


def link_color() -> QColor:
    """Цвет ссылки: тот же синий, что у обычных ссылок Windows."""
    return QColor("#1a5fb4")


# Правое Control: обе клавиши Control приходят в Qt одним Key_Control,
# поэтому сторону спрашиваем у самой Windows. Это единственный надёжный
# способ — в Qt 6 скан-код правой клавиши совпадает с левой.
VK_RCONTROL = 0xA3


def _user32():
    """Windows-функции для опроса клавиш, иначе None."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        return ctypes.windll.user32
    except Exception:
        return None


def _right_ctrl_down() -> bool:
    """Зажат ли правый Control прямо сейчас."""
    api = _user32()
    if api is None:
        return False
    try:
        return bool(api.GetAsyncKeyState(VK_RCONTROL) & 0x8000)
    except Exception:
        return False


class _ShortcutFilter(QObject):
    """Пользовательские сочетания клавиш на главном окне.

    Карту «сочетание — действие» собирает MainWindow._apply_shortcuts.
    Назначенное сочетание работает в любом месте программы, но в полях
    ввода уступает самому полю: иначе нельзя ни выделить, ни скопировать,
    ни вставить текст.
    """

    # Клавиши, у которых в поле ввода есть своя работа.
    EDITOR_KEYS = (
        Qt.Key_A, Qt.Key_C, Qt.Key_V, Qt.Key_X, Qt.Key_Z, Qt.Key_Y,
        Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
        Qt.Key_Home, Qt.Key_End, Qt.Key_Backspace, Qt.Key_Delete,
    )

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window

    @staticmethod
    def _is_editable_field(focus) -> bool:
        if isinstance(focus, (QLineEdit, QAbstractSpinBox)):
            return True
        if isinstance(focus, (QPlainTextEdit, QTextEdit)):
            return not focus.isReadOnly()
        return False

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        # Правое Control — «нажал, сказал, отпустил». Обе клавиши Control
        # приходят одним Key_Control, поэтому сторону разбираем по
        # системному коду; отпускание так же возвращает микрофон в mute.
        if kind == QEvent.Type.KeyPress and self.window._ptt_press(event):
            return False
        if kind == QEvent.Type.KeyRelease:
            self.window._ptt_release(event)
            return False
        if kind != QEvent.KeyPress:
            return False
        if event.isAutoRepeat() or not self.window.isActiveWindow():
            return False
        # Delete на выбранном треке. Не сочетание, а прямая клавиша списка:
        # кнопки «Удалить трек» больше нет, удаление подтверждается.
        if (event.key() == Qt.Key.Key_Delete
                and not (event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier)
                and self.window._delete_key_targets_track()):
            self.window._delete_track()
            return True
        if not self.window._shortcut_map:
            return False
        # Пока человек набирает текст, обычные клавиши остаются за полем
        # ввода: буквы, цифры, цифры с нумпада.
        if self._is_editable_field(QApplication.focusWidget()):
            typed = event.text()
            if event.key() in self.EDITOR_KEYS or (typed and typed.isprintable()):
                return False
        combo = shortcuts.combo_from_event(event)
        action = self.window._shortcut_map.get(combo)
        if not action:
            return False
        self.window._run_shortcut(action)
        return True


class _KeyCapture(QLineEdit):
    """Поле, которое запоминает нажатое сочетание как есть.

    QKeySequenceEdit не годится: клавишу цифрового блока он записывает
    как обычную цифру, а NumLock без Control вообще отбрасывает. Здесь
    сочетание собирается из самого нажатия.
    """

    def __init__(self, combo: str, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self._combo = combo or ""
        self.setText(shortcuts.pretty(self._combo) or "не задано")

    def combo(self) -> str:
        return self._combo

    def keyPressEvent(self, event) -> None:
        # Tab и Esc остаются навигацией: по ним диктор уходит с поля.
        if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab,
                           Qt.Key.Key_Escape):
            event.ignore()
            return
        text = shortcuts.combo_from_event(event)
        if not text:
            event.ignore()
            return
        self._combo = text
        self.setText(shortcuts.pretty(text))
        announce(self, f"Сочетание: {shortcuts.pretty(text)}.")
        event.accept()


class ShortcutDialog(QDialog):
    """Окно ввода сочетания клавиш.

    Открывается Enter на действии в списке. Внутри одно поле и две
    кнопки: диктору легко пройти Tab до «Принять» и нажать Пробел.
    """

    def __init__(self, title: str, combo: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Сочетание клавиш")
        self.setAccessibleName(f"Сочетание клавиш для действия {title}")
        self.setModal(True)
        layout = QVBoxLayout(self)
        info = QLabel(f"{title}.\nНажмите нужное сочетание клавиш, "
                      f"например Control+M.")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.edit = _KeyCapture(combo, self)
        self.edit.setAccessibleName("Новое сочетание клавиш")
        self.edit.setAccessibleDescription(
            "Нажмите сочетание: Control, Alt, Нумлок или клавиша цифрового "
            "блока, или клавиша с F1 по F12. Потом Tab до кнопки «Принять» "
            "и Пробел.")
        layout.addWidget(label_for("Новое сочетание", self.edit))
        layout.addWidget(self.edit)
        self.edit.setFocus()

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName("Ошибка ввода сочетания")
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok
                                   | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Принять")
        self.ok_button.setAccessibleName("Принять сочетание")
        cancel = buttons.button(QDialogButtonBox.Cancel)
        cancel.setText("Отмена")
        cancel.setAccessibleName("Отмена")
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _try_accept(self) -> None:
        ok, error = shortcuts.text_is_safe(self.edit.combo())
        if not ok:
            self.error_label.setText(error)
            announce(self, error)
            self.edit.setFocus()
            return
        self.error_label.setText("")
        self.accept()

    def combo(self) -> str:
        return shortcuts.normalize(self.edit.combo())


def _mode_title(key: str) -> str:
    """Название режима канала, которого сейчас нет в списке драйвера.

    Драйвер на миг может показать карту с меньшим числом каналов, и
    сохранённый режим в список не попадает. Но выбор человека от этого не
    пропадает: режим остаётся отдельным пунктом — «какой выбрал, такой и
    будет», пока его не поменяют.
    """
    key = str(key or "")
    if key == "stereo":
        return "Стерео, канал 1 слева, канал 2 справа"
    if key.startswith("ch") and key[2:].isdigit():
        return f"Моно, канал {key[2:]}"
    return key


class SettingsDialog(QDialog):
    """Настройки. Открывается по Control+P, дерево на четыре раздела."""

    sig_update = Signal(dict)
    sig_progress = Signal(int, int)
    sig_download = Signal(dict)

    def __init__(self, cfg: dict, parent=None, is_teacher: bool | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.is_teacher = (bool(is_teacher) if is_teacher is not None
                           else cfg.get("role") == "teacher")
        # Сочетания правятся в копии: «Отмена» не должна ничего менять.
        self.shortcut_map = dict(cfg.get("shortcuts") or {})
        self._downloading = False
        self._progress_step = 0
        self.sig_update.connect(self._on_update_result)
        self.sig_progress.connect(self._on_progress)
        self.sig_download.connect(self._on_download_done)
        self.setWindowTitle("Настройки")
        self.setAccessibleName("Настройки «Мажорный лад»")
        self.resize(720, 520)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName("Разделы настроек")
        self.stack = QStackedWidget()
        self.stack.setAccessibleName("Содержимое раздела")

        sections = [
            ("Общее", self._page_general()),
            ("Плеер", self._page_player()),
            ("Аудиовходы", self._page_inputs()),
            ("Аудиовыходы", self._page_outputs()),
            ("Видео", self._page_video()),
            ("Сочетания клавиш", self._page_shortcuts()),
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

    def _refresh_midi_ports(self, announce_it: bool = True) -> None:
        """Перечитывает список MIDI-устройств на ходу.

        Клавиатуру часто включают уже после запуска программы, поэтому
        список нужно уметь обновлять, не закрывая настройки.
        """
        current = self.midi_combo.currentData() or ""
        ports = _midi_ports()
        self.midi_combo.clear()
        self.midi_combo.addItem("Первое доступное", "")
        for port in ports:
            self.midi_combo.addItem(port, port)
        index = self.midi_combo.findData(current)
        self.midi_combo.setCurrentIndex(index if index >= 0 else 0)
        if not announce_it:
            return
        if ports:
            announce(self.midi_combo,
                     "Найдено MIDI-устройств: " + ", ".join(ports))
        else:
            announce(self.midi_combo,
                     "MIDI-устройства не найдены. Проверь, что клавиатура "
                     "включена и подключена, и нажми «Обновить» ещё раз.")

    def _midi_options(self) -> dict:
        """Выбор синтезатора из настроек — под ключи файла настроек."""
        return {
            "midi_wave": str(self.midi_wave_combo.currentData() or "sine"),
            "midi_dynamics": str(
                self.midi_dynamics_combo.currentData() or "velocity"),
            "midi_pedal": bool(self.midi_pedal_combo.currentData()),
            "midi_attack_ms": int(self.midi_attack_spin.value()),
            "midi_decay_ms": int(self.midi_decay_spin.value()),
            "midi_sustain": int(self.midi_sustain_spin.value()),
            "midi_release_ms": int(self.midi_release_spin.value()),
        }

    def _open_sampler_folder(self) -> None:
        """Открывает Data\\Sampler в проводнике: туда кладут файлы семплов."""
        try:
            from .midi_synth import sampler_dir

            folder = sampler_dir()
        except Exception as exc:
            QMessageBox.warning(self, "Семплер",
                                f"Не удалось открыть папку семплов: {exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.information(
                self, "Семплер",
                f"Папка семплов: {folder}\n\n"
                f"Положи туда файлы 001.wav … 127.wav: номер файла — "
                f"номер ноты.")

    def _page_general(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Общее")
        form = QFormLayout(page)

        self.role_label = QLabel("Преподаватель" if self.cfg.get("role") == "teacher" else "Ученик")
        self.role_label.setAccessibleName("Роль в программе")
        form.addRow(label_for("Роль"), self.role_label)

        self.name_edit = QLineEdit(self.cfg.get("name", ""))
        self.name_edit.setAccessibleName("Ваше имя")
        self.name_edit.setPlaceholderText("Ваше имя")
        form.addRow(label_for("Имя", self.name_edit), self.name_edit)

        self.ip_edit = QLineEdit(self.cfg.get("server_ip", ""))
        self.ip_edit.setAccessibleName("Адрес преподавателя")
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
        self.update_button.clicked.connect(self._check_updates)
        version_row.addWidget(self.version_label, 1)
        version_row.addWidget(self.update_button)
        form.addRow(version_row)

        self.update_status = QLabel("")
        self.update_status.setWordWrap(True)
        self.update_status.setAccessibleName("Состояние обновления")
        form.addRow(self.update_status)

        return page

    def _page_player(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Плеер")
        form = QFormLayout(page)
        player_cfg = self.cfg.get("player") or {}

        skip_explain = QLabel(
            "На сколько секунд перематывают кнопки «Назад» и «Вперёд».")
        skip_explain.setWordWrap(True)
        skip_explain.setAccessibleName("Шаг перемотки плеера, пояснение")
        form.addRow(skip_explain)

        self.skip_combo = QComboBox()
        self.skip_combo.setAccessibleName("Шаг перемотки плеера")
        for seconds in (5, 10, 20):
            self.skip_combo.addItem(f"{seconds} секунд", seconds)
        saved_skip = int(player_cfg.get("skip", 5) or 5)
        skip_index = self.skip_combo.findData(saved_skip)
        self.skip_combo.setCurrentIndex(skip_index if skip_index >= 0 else 0)
        form.addRow(label_for("Шаг перемотки плеера", self.skip_combo),
                    self.skip_combo)

        algo_explain = QLabel(
            "Способ, которым программа меняет скорость и тон трека. "
            "«Плавный» звучит ровнее, особенно на больших растяжениях. "
            "«Чёткий» лучше держит атаку удара по струне, но на сильном "
            "замедлении слышны небольшие повторы.")
        algo_explain.setWordWrap(True)
        algo_explain.setAccessibleName("Способ растяжения трека, пояснение")
        form.addRow(algo_explain)

        self.stretch_combo = QComboBox()
        self.stretch_combo.setAccessibleName("Способ растяжения трека")
        self.stretch_combo.addItem("Плавный, как в версии 0.4.7", "vocoder")
        self.stretch_combo.addItem("Чёткий, с ясной атакой", "wsola")
        saved_algo = str(player_cfg.get("stretch", effects.DEFAULT_ALGO)
                         or effects.DEFAULT_ALGO)
        algo_index = self.stretch_combo.findData(saved_algo)
        self.stretch_combo.setCurrentIndex(algo_index if algo_index >= 0 else 0)
        form.addRow(label_for("Способ растяжения", self.stretch_combo),
                    self.stretch_combo)
        return page

    def _device_combo(self, current: str, name: str,
                      values: list[str]) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(False)
        combo.setAccessibleName(name)
        self._fill_device_combo(combo, "Не выбрано", values, current,
                                default_label=DEFAULT_MIC_LABEL)
        return combo

    def _fill_device_combo(self, combo: QComboBox, first_label: str,
                           values: list[str], keep: str = "",
                           default_label: str = "",
                           reset: bool = False) -> None:
        """Перезаполняет список устройств, сохраняя выбранное.

        Выбранного устройства может не оказаться в новом списке — например,
        когда оно спрятано фильтром драйверов. Молча сбросить выбор нельзя:
        человек нажмёт «Сохранить» и потеряет устройство. Поэтому оставляем
        его отдельным пунктом. reset=True — список пересобран под другой
        драйвер, и старое имя к нему уже не относится.
        """
        if reset:
            keep = ""
        elif not keep:
            keep = str(combo.currentData() or "")
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(first_label, "")
        if default_label:
            # Пункт «системный по умолчанию» не привязан к имени устройства:
            # на другом компьютере имя другое, а этот пункт всегда работает.
            combo.addItem(default_label, config.DEFAULT_DEVICE)
        for dev in values:
            combo.addItem(dev, dev)
        index = combo.findData(keep)
        if keep and index < 0:
            combo.addItem(keep, keep)
            index = combo.count() - 1
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _refresh_device_lists(self, show_all: bool,
                              reset: bool = False) -> None:
        """Перезаполняет списки устройств под выбранный драйвер.

        Для ASIO в списках стоит не отдельный микрофон, а звуковая карта
        целиком: драйвер один на карту, вход и выход у неё общие.
        """
        self.cfg["audio"]["show_all_drivers"] = bool(show_all)
        driver = self._driver_value()
        if driver == "asio":
            inputs = audio.asio_devices("input")
            outputs = audio.asio_devices("output")
        else:
            inputs = audio.list_input_devices(show_all)
            outputs = audio.list_output_devices(show_all)
        for combo in (self.dev1_combo, self.dev2_combo, self.usb_combo):
            self._fill_device_combo(combo, "Не выбрано", inputs,
                                    default_label=DEFAULT_MIC_LABEL,
                                    reset=reset)
        self._fill_device_combo(self.out_combo, "Системное по умолчанию",
                                outputs, reset=reset)
        self._refresh_mode_combos()

    def _refresh_mode_combos(self) -> None:
        """Пересобирает режимы каналов: у ASIO их столько, сколько даёт карта."""
        driver = self._driver_value()
        channels = 0
        if driver == "asio":
            info = audio.asio_device_info(
                str(self.asio_combo.currentData() or ""))
            channels = info[0] if info else 0
        for combo in (self.mode1_combo, self.mode2_combo, self.usb_mode_combo):
            current = str(combo.currentData() or "ch1")
            combo.blockSignals(True)
            combo.clear()
            for key, title in audio.device_modes(driver, "", channels):
                combo.addItem(title, key)
            index = combo.findData(current)
            if index < 0 and current:
                # Карта на миг показала меньше каналов — сохранённый режим
                # всё равно остаётся выбранным, а не сбрасывается на первый.
                combo.addItem(_mode_title(current), current)
                index = combo.count() - 1
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def _driver_value(self) -> str:
        return str(self.driver_combo.currentData() or "wasapi")

    def _driver_changed(self, *_args, reset: bool = True) -> None:
        """Показывает поля ASIO и перезаполняет списки под новый драйвер.

        reset выключается при первом открытии настроек: там сохранённые
        устройства надо оставить на месте, а не стирать вместе с именами
        чужих драйверов.
        """
        asio = self._driver_value() == "asio"
        for widget in (getattr(self, "asio_card_label", None),
                       getattr(self, "asio_combo", None),
                       getattr(self, "asio_in_label", None),
                       getattr(self, "asio_in_spin", None),
                       getattr(self, "asio_out_label", None),
                       getattr(self, "asio_out_spin", None),
                       getattr(self, "asio_hint", None)):
            if widget is not None:
                widget.setVisible(asio)
        if hasattr(self, "all_drivers_box"):
            # «Устаревшие драйверы» — только про WASAPI: у ASIO список
            # свой, решать там нечего.
            self.all_drivers_box.setEnabled(not asio)
        if asio:
            names = audio.asio_devices("input") or audio.asio_devices("output")
            keep = (self.asio_combo.currentData()
                    or str(self.cfg["audio"].get("asio_device", "")))
            self.asio_combo.blockSignals(True)
            self.asio_combo.clear()
            for name in names:
                self.asio_combo.addItem(name, name)
            index = self.asio_combo.findData(keep)
            self.asio_combo.setCurrentIndex(index if index >= 0 else 0)
            self.asio_combo.blockSignals(False)
            self._asio_card_changed()
        if hasattr(self, "out_combo"):
            self._refresh_device_lists(
                bool(self.cfg["audio"].get("show_all_drivers", False)),
                reset=reset)
        if asio:
            # Карта одна на всё: подставляем её в первый вход и выход,
            # чтобы человек не искал её ещё в двух вкладках.
            card = self.asio_combo.currentData()
            for combo in (self.dev1_combo, self.out_combo):
                if not combo.currentData():
                    index = combo.findData(card)
                    if index >= 0:
                        combo.setCurrentIndex(index)

    def _asio_card_changed(self, *_args) -> None:
        """Подгоняет счётчики каналов под выбранную карту ASIO."""
        info = audio.asio_device_info(str(self.asio_combo.currentData() or ""))
        if info:
            self.asio_in_spin.setMaximum(max(1, info[0]))
            self.asio_out_spin.setMaximum(max(1, info[1]))
        if hasattr(self, "mode1_combo"):
            self._refresh_mode_combos()

    def _page_inputs(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Аудиовходы")
        layout = QVBoxLayout(page)

        tabs = QTabWidget()
        tabs.setAccessibleName("Вкладки аудиовходов")

        show_all = bool(self.cfg["audio"].get("show_all_drivers", False))
        inputs = audio.list_input_devices(show_all)

        driver_page = QWidget()
        driver_form = QFormLayout(driver_page)
        self.driver_combo = QComboBox()
        self.driver_combo.setAccessibleName("Драйвер звука")
        self.driver_combo.setAccessibleDescription(
            "WASAPI — обычные драйверы Windows: карта работает не только в "
            "этой программе. ASIO — драйвер звуковой карты с низкой "
            "задержкой: пока программа запущена, карту видят только она.")
        for key, title in audio.list_audio_drivers():
            self.driver_combo.addItem(title, key)
        index = self.driver_combo.findData(
            str(self.cfg["audio"].get("driver", "wasapi")))
        self.driver_combo.setCurrentIndex(index if index >= 0 else 0)
        driver_form.addRow(label_for("Драйвер", self.driver_combo),
                           self.driver_combo)

        self.asio_combo = QComboBox()
        self.asio_combo.setAccessibleName("Звуковая карта ASIO")
        self.asio_combo.setAccessibleDescription(
            "Карта, которую программа забирает через ASIO. Пока идёт урок, "
            "другие программы её не видят.")
        self.asio_card_label = label_for("Звуковая карта",
                                         self.asio_combo)
        driver_form.addRow(self.asio_card_label, self.asio_combo)

        self.asio_in_spin = QSpinBox()
        self.asio_in_spin.setRange(1, 32)
        self.asio_in_spin.setValue(max(1, int(
            self.cfg["audio"].get("asio_in_channels", 2) or 2)))
        self.asio_in_spin.setAccessibleName("Сколько входных каналов открыть")
        self.asio_in_spin.setAccessibleDescription(
            "Сколько входов звуковой карты программа забирает. Обычно "
            "хватает двух: стереопара.")
        self.asio_in_label = label_for("Входных каналов", self.asio_in_spin)
        driver_form.addRow(self.asio_in_label, self.asio_in_spin)

        self.asio_out_spin = QSpinBox()
        self.asio_out_spin.setRange(1, 32)
        self.asio_out_spin.setValue(max(1, int(
            self.cfg["audio"].get("asio_out_channels", 2) or 2)))
        self.asio_out_spin.setAccessibleName("Сколько выходных каналов открыть")
        self.asio_out_spin.setAccessibleDescription(
            "Сколько выходов звуковой карты программа забирает: в них идут "
            "наушники, метроном и минусовка.")
        self.asio_out_label = label_for("Выходных каналов", self.asio_out_spin)
        driver_form.addRow(self.asio_out_label, self.asio_out_spin)

        self.device_buffer_combo = QComboBox()
        self.device_buffer_combo.setAccessibleName("Размер буфера драйвера")
        self.device_buffer_combo.setAccessibleDescription(
            "Размер блока, который программа просит у драйвера. Меньше "
            "блок — меньше задержка, но выше нагрузка: на 32 и 48 может "
            "появляться треск. У карт ASIO точный размер принадлежит "
            "драйверу: Focusrite ставит ближайший допустимый, и запрос 32 "
            "может превратиться в 48. Точный размер виден в панели "
            "драйвера. «По драйверу» — ничего не просить, как было раньше.")
        for value, title in ((0, "По драйверу"), (32, "32 отсчёта"),
                             (48, "48 отсчётов"), (64, "64 отсчёта"),
                             (96, "96 отсчётов"), (128, "128 отсчётов"),
                             (192, "192 отсчёта"), (256, "256 отсчётов"),
                             (512, "512 отсчётов")):
            self.device_buffer_combo.addItem(title, value)
        buf_index = self.device_buffer_combo.findData(
            int(self.cfg["audio"].get("device_buffer", 0) or 0))
        self.device_buffer_combo.setCurrentIndex(
            buf_index if buf_index >= 0 else 0)
        driver_form.addRow(label_for("Буфер драйвера",
                                     self.device_buffer_combo),
                           self.device_buffer_combo)

        asio_hint = QLabel(
            "ASIO занимает карту целиком: другие программы во время урока "
            "звук через неё не получат. Какие именно каналы брать, "
            "выбирается во вкладках устройств.")
        asio_hint.setWordWrap(True)
        asio_hint.setAccessibleName("Подсказка про ASIO")
        driver_form.addRow(asio_hint)
        self.asio_hint = asio_hint

        tabs.addTab(driver_page, "Драйвер")

        self.dev1_combo = self._device_combo(
            self.cfg["audio"].get("device1", ""), "Аудиоустройство 1", inputs)
        self.mode1_combo = self._mode_combo(
            self.cfg["audio"].get("mode1", "ch1"), "Режим канала 1")
        tabs.addTab(self._device_tab(self.dev1_combo, self.mode1_combo,
                                     with_fx=True),
                    "Аудиоустройство 1")

        self.dev2_combo = self._device_combo(
            self.cfg["audio"].get("device2", ""), "Аудиоустройство 2", inputs)
        self.mode2_combo = self._mode_combo(
            self.cfg["audio"].get("mode2", "ch1"), "Режим канала 2")
        tabs.addTab(self._device_tab(self.dev2_combo, self.mode2_combo),
                    "Аудиоустройство 2")

        midi_page = QWidget()
        midi_form = QFormLayout(midi_page)
        self.midi_box = QCheckBox("Использовать MIDI-клавиатуру")
        self.midi_box.setChecked(bool(self.cfg["audio"].get("midi_enabled", False)))
        self.midi_box.setAccessibleName("Использовать MIDI-клавиатуру")
        midi_form.addRow(self.midi_box)

        self.midi_combo = QComboBox()
        self.midi_combo.setAccessibleName("MIDI-устройство")
        midi_row = QWidget()
        midi_row_layout = QHBoxLayout(midi_row)
        midi_row_layout.setContentsMargins(0, 0, 0, 0)
        midi_row_layout.addWidget(self.midi_combo, 1)
        refresh = QPushButton("Обновить")
        refresh.setAccessibleName("Обновить список MIDI-устройств")
        refresh.clicked.connect(self._refresh_midi_ports)
        midi_row_layout.addWidget(refresh)
        self._refresh_midi_ports(announce_it=False)
        selected = str(self.cfg["audio"].get("midi_port", ""))
        index = self.midi_combo.findData(selected)
        if index < 0 and selected:
            # Устройство было выбрано, но сейчас его нет — запоминаем выбор,
            # чтобы оно подхватилось, когда его включат.
            self.midi_combo.addItem(f"{selected} (не найдено)", selected)
            index = self.midi_combo.count() - 1
        self.midi_combo.setCurrentIndex(index if index >= 0 else 0)
        midi_form.addRow(label_for("Порт", self.midi_combo), midi_row)

        audio_cfg = self.cfg["audio"]
        self.midi_wave_combo = QComboBox()
        self.midi_wave_combo.setAccessibleName("Звук синтезатора")
        for label, key in (("Синус", "sine"), ("Треугольник", "triangle"),
                           ("Квадрат", "square"), ("Семплер", "sampler")):
            self.midi_wave_combo.addItem(label, key)
        wave = str(audio_cfg.get("midi_wave", "sine"))
        wave_index = self.midi_wave_combo.findData(wave)
        self.midi_wave_combo.setCurrentIndex(wave_index if wave_index >= 0 else 0)
        midi_form.addRow(label_for("Звук", self.midi_wave_combo),
                         self.midi_wave_combo)

        sampler_row = QWidget()
        sampler_layout = QHBoxLayout(sampler_row)
        sampler_layout.setContentsMargins(0, 0, 0, 0)
        sampler_hint = QLabel("Файлы 001.wav … 127.wav в папке Data\\Sampler. "
                              "Номер файла — номер ноты. Нет файла — нота молчит.")
        sampler_hint.setWordWrap(True)
        sampler_hint.setAccessibleName(sampler_hint.text())
        sampler_layout.addWidget(sampler_hint, 1)
        sampler_open = QPushButton("Открыть папку")
        sampler_open.setAccessibleName("Открыть папку семплов")
        sampler_open.clicked.connect(self._open_sampler_folder)
        sampler_layout.addWidget(sampler_open)
        midi_form.addRow(sampler_row)

        self.midi_dynamics_combo = QComboBox()
        self.midi_dynamics_combo.setAccessibleName("Громкость нот")
        self.midi_dynamics_combo.addItem("Без динамики", "fixed")
        self.midi_dynamics_combo.addItem("С динамикой", "velocity")
        dynamics = str(audio_cfg.get("midi_dynamics", "velocity"))
        dyn_index = self.midi_dynamics_combo.findData(dynamics)
        self.midi_dynamics_combo.setCurrentIndex(
            dyn_index if dyn_index >= 0 else 1)
        midi_form.addRow(label_for("Громкость нот", self.midi_dynamics_combo),
                         self.midi_dynamics_combo)

        self.midi_pedal_combo = QComboBox()
        self.midi_pedal_combo.setAccessibleName("Педаль")
        self.midi_pedal_combo.addItem("Педаль не нажата", False)
        self.midi_pedal_combo.addItem("Педаль нажата", True)
        self.midi_pedal_combo.setCurrentIndex(
            1 if bool(audio_cfg.get("midi_pedal", False)) else 0)
        midi_form.addRow(label_for("Педаль", self.midi_pedal_combo),
                         self.midi_pedal_combo)

        self.midi_attack_spin = QSpinBox()
        self.midi_attack_spin.setRange(0, 5000)
        self.midi_attack_spin.setSingleStep(10)
        self.midi_attack_spin.setSuffix(" мс")
        self.midi_attack_spin.setValue(int(audio_cfg.get("midi_attack_ms", 6)))
        self.midi_attack_spin.setAccessibleName("Атака")
        midi_form.addRow(label_for("Атака", self.midi_attack_spin),
                         self.midi_attack_spin)

        self.midi_decay_spin = QSpinBox()
        self.midi_decay_spin.setRange(0, 5000)
        self.midi_decay_spin.setSingleStep(10)
        self.midi_decay_spin.setSuffix(" мс")
        self.midi_decay_spin.setValue(int(audio_cfg.get("midi_decay_ms", 0)))
        self.midi_decay_spin.setAccessibleName("Декай")
        midi_form.addRow(label_for("Декай", self.midi_decay_spin),
                         self.midi_decay_spin)

        self.midi_sustain_spin = QSpinBox()
        self.midi_sustain_spin.setRange(0, 100)
        self.midi_sustain_spin.setSuffix(" %")
        self.midi_sustain_spin.setValue(int(audio_cfg.get("midi_sustain", 100)))
        self.midi_sustain_spin.setAccessibleName("Сустейн")
        midi_form.addRow(label_for("Сустейн", self.midi_sustain_spin),
                         self.midi_sustain_spin)

        self.midi_release_spin = QSpinBox()
        self.midi_release_spin.setRange(0, 5000)
        self.midi_release_spin.setSingleStep(10)
        self.midi_release_spin.setSuffix(" мс")
        self.midi_release_spin.setValue(int(audio_cfg.get("midi_release_ms", 45)))
        self.midi_release_spin.setAccessibleName("Релиз")
        midi_form.addRow(label_for("Релиз", self.midi_release_spin),
                         self.midi_release_spin)
        tabs.addTab(midi_page, "MIDI-устройства")

        self.usb_combo = self._device_combo(
            self.cfg["audio"].get("usb", ""), "USB-устройство", inputs)
        self.usb_mode_combo = self._mode_combo(
            self.cfg["audio"].get("usb_mode", "ch1"), "Режим канала USB")
        tabs.addTab(self._device_tab(self.usb_combo, self.usb_mode_combo),
                    "USB-устройство")

        self.driver_combo.currentIndexChanged.connect(self._driver_changed)
        self.asio_combo.currentIndexChanged.connect(self._asio_card_changed)

        layout.addWidget(tabs)

        self.all_drivers_box = QCheckBox("Показывать устаревшие драйверы")
        self.all_drivers_box.setChecked(show_all)
        self.all_drivers_box.setAccessibleName("Показывать устаревшие драйверы")
        self.all_drivers_box.setAccessibleDescription(
            "Обычно в списках только WASAPI — современный драйвер Windows с "
            "малой задержкой. Включи, если нужного устройства в списке нет.")
        self.all_drivers_box.toggled.connect(self._refresh_device_lists)
        layout.addWidget(self.all_drivers_box)

        hint = QLabel(
            "Список устройств обновляется при открытии настроек. "
            "Новую звуковую карту лучше подключить до запуска программы."
        )
        hint.setWordWrap(True)
        hint.setAccessibleName("Подсказка")
        layout.addWidget(hint)
        return page

    def _mode_combo(self, current: str, name: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(False)
        combo.setAccessibleName(name)
        combo.addItem("Моно, канал 1", "ch1")
        combo.addItem("Моно, канал 2", "ch2")
        combo.addItem("Стерео, канал 1 слева, канал 2 справа", "stereo")
        index = combo.findData(current)
        if index < 0 and current:
            # Сохранённый режим, которого нет в списке (например, «канал 5»
            # у карты, которая сейчас показывает только два канала), —
            # отдельным пунктом, чтобы выбор не сбросился на первый.
            combo.addItem(_mode_title(current), current)
            index = combo.count() - 1
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.setAccessibleDescription(
            "Стерео берёт оба входа звуковой карты сразу: первый идёт в левое "
            "ухо, второй — в правое.")
        return combo

    def _device_tab(self, combo: QComboBox,
                    mode_combo: QComboBox | None = None,
                    with_fx: bool = False) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        combo_label = label_for("Устройство", combo)
        form.addRow(combo_label, combo)
        if mode_combo is not None:
            form.addRow(label_for("Режим", mode_combo), mode_combo)
        if with_fx:
            audio_cfg = self.cfg["audio"]
            self.monitor1_box = QCheckBox("Слышать себя")
            self.monitor1_box.setChecked(
                bool(audio_cfg.get("monitor1", False)))
            self.monitor1_box.setAccessibleName("Слышать себя")
            self.monitor1_box.setAccessibleDescription(
                "Звук первого входа идёт и в наушники, и ученику. Громкость "
                "в наушниках — регулятором «Аудиоустройство 1» на главном "
                "экране.")
            form.addRow(self.monitor1_box)

            self.lowcut_box = QCheckBox("Срез низких частот 150 Гц")
            self.lowcut_box.setChecked(bool(audio_cfg.get("lowcut", False)))
            self.lowcut_box.setAccessibleName("Срез низких частот 150 Гц")
            self.lowcut_box.setAccessibleDescription(
                "Убирает всё ниже 150 Гц: гул комнаты, стук по корпусу и "
                "рокот. Голос и гитара становятся чище. Голос звучит почти "
                "так же, а гудение уходит.")
            form.addRow(self.lowcut_box)

            self.compressor_spin = QSpinBox()
            self.compressor_spin.setRange(0, 100)
            self.compressor_spin.setSingleStep(10)
            self.compressor_spin.setSuffix(" %")
            self.compressor_spin.setValue(
                max(0, min(100, int(audio_cfg.get("compressor", 0) or 0))))
            self.compressor_spin.setAccessibleName("Компрессор")
            self.compressor_spin.setAccessibleDescription(
                "Выравнивает громкость: громкие места приглушаются, тихие "
                "становятся слышнее. Один регулятор: чем больше, тем ровнее "
                "и громче тихое. 0 — выключен.")
            form.addRow(label_for("Компрессор", self.compressor_spin),
                        self.compressor_spin)
        return page

    def _page_outputs(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Аудиовыходы")
        form = QFormLayout(page)
        self.out_combo = QComboBox()
        self.out_combo.setAccessibleName("Устройство вывода")
        show_all = bool(self.cfg["audio"].get("show_all_drivers", False))
        self._fill_device_combo(
            self.out_combo, "Системное по умолчанию",
            audio.list_output_devices(show_all),
            str(self.cfg["audio"].get("output", "")))
        form.addRow(label_for("Устройство вывода", self.out_combo), self.out_combo)

        # Запас звука на сеть: больше — устойчивее и с более длинной
        # задержкой, меньше — отзывчивее и чувствительнее к провалам связи.
        self.buffer_combo = QComboBox()
        self.buffer_combo.setAccessibleName("Размер буфера звука")
        for key, title in (("low", "Минимальная задержка, 20–40 мс"),
                           ("normal", "Обычная, 40–80 мс"),
                           ("stable", "Устойчивая, 80–160 мс")):
            self.buffer_combo.addItem(title, key)
        index = self.buffer_combo.findData(
            str(self.cfg["audio"].get("buffer", "normal")))
        self.buffer_combo.setCurrentIndex(index if index >= 0 else 1)
        form.addRow(label_for("Размер буфера звука", self.buffer_combo),
                    self.buffer_combo)

        # Битрейт Opus. Действует на исходящий поток: чем выше, тем лучше
        # качество и тем больше данных нужно каналу. Для урока хватает 96.
        self.opus_bitrate_combo = QComboBox()
        self.opus_bitrate_combo.setAccessibleName("Битрейт сжатия звука")
        self.opus_bitrate_combo.setAccessibleDescription(
            "Сколько данных уходит собеседнику. 96 кбит/с — для урока "
            "достаточно, 128–320 — если канал позволяет. Работает, когда "
            "оба конца поддерживают сжатие Opus.")
        for kbps in (96, 128, 192, 320):
            self.opus_bitrate_combo.addItem(f"{kbps} кбит/с", kbps)
        index = self.opus_bitrate_combo.findData(
            int(self.cfg["audio"].get("opus_bitrate", 96) or 96))
        self.opus_bitrate_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow(label_for("Битрейт сжатия звука",
                              self.opus_bitrate_combo),
                    self.opus_bitrate_combo)

        # Компенсация задержки: клик, MIDI и трек ждут, пока голос пройдёт
        # микрофон и входной буфер, и только потом уходят собеседнику.
        # Ноль — программа считает задержку сама по открытым устройствам.
        self.tx_delay_spin = QSpinBox()
        self.tx_delay_spin.setRange(0, 300)
        self.tx_delay_spin.setSingleStep(5)
        self.tx_delay_spin.setSuffix(" мс")
        self.tx_delay_spin.setSpecialValueText("Автоматически")
        self.tx_delay_spin.setValue(
            int(self.cfg["audio"].get("tx_delay_ms", 0) or 0))
        self.tx_delay_spin.setAccessibleName("Компенсация задержки")
        self.tx_delay_spin.setAccessibleDescription(
            "Ноль — программа сама измеряет задержку микрофона и буфера. "
            "Если клик у собеседника всё равно опережает хлопок, увеличьте "
            "значение на 10–30 мс.")
        form.addRow(label_for("Компенсация задержки", self.tx_delay_spin),
                    self.tx_delay_spin)

        hint = QLabel(
            "Для занятий нужны наушники: без них микрофон поймает колонки "
            "и вернёт эхо собеседнику."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        # Настройки открыты: применяем сохранённый драйвер — поля ASIO
        # показываем или прячем, списки устройств собираем под него.
        # reset=False, иначе сотрём только что выбранные устройства.
        self._driver_changed(reset=False)
        return page

    def _page_video(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Видео")
        form = QFormLayout(page)

        self.video_box = QCheckBox("Включить видео")
        self.video_box.setChecked(bool(self.cfg["video"].get("enabled", False)))
        self.video_box.setAccessibleName("Включить видео")
        form.addRow(self.video_box)

        # Список камер с именами: «Встроенная камера», «USB-камера».
        # Номер OpenCV спрятан в данных пункта. Камер не видно — остаётся
        # обычная нумерация, как было раньше.
        self.cam_combo = QComboBox()
        self.cam_combo.setAccessibleName("Камера")
        saved_camera = int(self.cfg["video"].get("camera", 0))
        devices = cameras.list_cameras()
        if devices:
            for name, index in devices:
                self.cam_combo.addItem(name, index)
            chosen = self.cam_combo.findData(saved_camera)
            if chosen < 0:
                # Сохранённой камеры сейчас нет: не подменяем её молча
                # другой, а показываем её отдельным пунктом.
                self.cam_combo.addItem(f"Камера {saved_camera + 1}",
                                       saved_camera)
                chosen = self.cam_combo.count() - 1
            self.cam_combo.setCurrentIndex(chosen)
        else:
            for index in range(4):
                self.cam_combo.addItem(f"Камера {index + 1}", index)
            self.cam_combo.setCurrentIndex(
                min(saved_camera, self.cam_combo.count() - 1))
        self.cam_combo.setAccessibleDescription(
            "Список подключённых камер. Если нужной камеры нет, подключите "
            "её до открытия настроек.")
        form.addRow(label_for("Камера", self.cam_combo), self.cam_combo)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(int(self.cfg["video"].get("fps", 15)))
        self.fps_spin.setAccessibleName("Кадров в секунду")
        form.addRow(label_for("Кадров в секунду", self.fps_spin), self.fps_spin)

        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(20, 95)
        self.quality_spin.setValue(int(self.cfg["video"].get("quality", 60)))
        self.quality_spin.setAccessibleName("Качество видео")
        form.addRow(label_for("Качество", self.quality_spin), self.quality_spin)

        self.video_width_spin = QSpinBox()
        self.video_width_spin.setRange(160, 1920)
        self.video_width_spin.setSingleStep(16)
        self.video_width_spin.setValue(int(self.cfg["video"].get("width", 640)))
        self.video_width_spin.setAccessibleName("Ширина видео")
        form.addRow(label_for("Ширина", self.video_width_spin),
                    self.video_width_spin)

        self.video_height_spin = QSpinBox()
        self.video_height_spin.setRange(120, 1080)
        self.video_height_spin.setSingleStep(8)
        self.video_height_spin.setValue(int(self.cfg["video"].get("height", 480)))
        self.video_height_spin.setAccessibleName("Высота видео")
        form.addRow(label_for("Высота", self.video_height_spin),
                    self.video_height_spin)

        # Предел видеопотока. Без предела JPEG сам выбирает размер кадра,
        # а с пределом качество подстраивается под него.
        self.video_bitrate_combo = QComboBox()
        self.video_bitrate_combo.setAccessibleName("Предел видеопотока")
        for title, rate in (("Без предела", 0), ("128 кбит/с", 128),
                            ("256 кбит/с", 256), ("512 кбит/с", 512),
                            ("1 Мбит/с", 1000), ("2 Мбит/с", 2000),
                            ("4 Мбит/с", 4000)):
            self.video_bitrate_combo.addItem(title, rate)
        index = self.video_bitrate_combo.findData(
            int(self.cfg["video"].get("bitrate", 0)))
        self.video_bitrate_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow(label_for("Предел потока", self.video_bitrate_combo),
                    self.video_bitrate_combo)

        self.show_remote_box = QCheckBox("Показывать видео собеседника")
        self.show_remote_box.setChecked(bool(self.cfg["video"].get("show_remote", True)))
        self.show_remote_box.setAccessibleName("Показывать видео собеседника")
        form.addRow(self.show_remote_box)

        return page

    def _page_shortcuts(self) -> QWidget:
        page = QWidget()
        page.setAccessibleName("Сочетания клавиш")
        layout = QVBoxLayout(page)

        hint = QLabel(
            "Выберите действие стрелками и нажмите Enter. В открывшемся "
            "окне нажмите нужное сочетание клавиш, затем Tab до кнопки "
            "«Принять» и Пробел. Подходят сочетания с Control, Alt, "
            "Нумлоком или клавишей цифрового блока, а также клавиши "
            "с F1 по F12."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.shortcut_list = QListWidget()
        self.shortcut_list.setAccessibleName("Действия и их сочетания")
        for action_id, _title in shortcuts.all_actions(self.is_teacher):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, action_id)
            self.shortcut_list.addItem(item)
        self.shortcut_list.itemActivated.connect(
            lambda _item: self._edit_shortcut())
        layout.addWidget(self.shortcut_list, 1)

        buttons = QHBoxLayout()
        self.assign_button = QPushButton("Изменить")
        self.assign_button.setAccessibleName(
            "Изменить сочетание выбранного действия")
        self.clear_button = QPushButton("Сбросить")
        self.clear_button.setAccessibleName(
            "Сбросить сочетание у выбранного действия")
        self.assign_button.clicked.connect(self._edit_shortcut)
        self.clear_button.clicked.connect(self._clear_shortcut)
        buttons.addWidget(self.assign_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._refresh_shortcut_list()
        self.shortcut_list.setCurrentRow(0)
        return page

    def _shortcut_action(self) -> str:
        item = self.shortcut_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def _refresh_shortcut_list(self) -> None:
        """Переписывает названия: у каждого действия — его сочетание."""
        titles = dict(shortcuts.all_actions(self.is_teacher))
        row = self.shortcut_list.currentRow()
        for index in range(self.shortcut_list.count()):
            item = self.shortcut_list.item(index)
            action_id = str(item.data(Qt.ItemDataRole.UserRole))
            combo = shortcuts.resolved(self.shortcut_map, action_id)
            state = shortcuts.pretty(combo) if combo else "не назначено"
            item.setText(f"{titles.get(action_id, action_id)}: {state}")
            item.setData(Qt.ItemDataRole.AccessibleTextRole,
                         item.text())
        if row >= 0:
            self.shortcut_list.setCurrentRow(row)

    def _edit_shortcut(self) -> None:
        """Enter на действии (или кнопка «Изменить») — окно ввода."""
        action_id = self._shortcut_action()
        if not action_id:
            announce(self.shortcut_list, "Сначала выберите действие в списке.")
            return
        titles = dict(shortcuts.all_actions(self.is_teacher))
        title = titles.get(action_id, action_id)
        combo = shortcuts.resolved(self.shortcut_map, action_id)
        dialog = ShortcutDialog(title, combo, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_shortcut(action_id, dialog.combo())

    def _apply_shortcut(self, action_id: str, combo: str) -> None:
        titles = dict(shortcuts.all_actions(self.is_teacher))
        # Одно сочетание — одно действие: у прежнего владельца оно
        # снимается, иначе нажатие срабатывало бы дважды.
        for other_id, _title in shortcuts.all_actions(self.is_teacher):
            if other_id == action_id:
                continue
            other = shortcuts.resolved(self.shortcut_map, other_id)
            if other == combo:
                self.shortcut_map[other_id] = shortcuts.UNSET
                announce(self.shortcut_list,
                         f"Сочетание {shortcuts.pretty(combo)} снято "
                         f"с действия «{titles.get(other_id, other_id)}».")
                break
        self.shortcut_map[action_id] = combo
        self._refresh_shortcut_list()
        announce(self.shortcut_list,
                 f"{titles.get(action_id, action_id)}: назначено "
                 f"{shortcuts.pretty(combo)}.")

    def _clear_shortcut(self) -> None:
        action_id = self._shortcut_action()
        if not action_id:
            return
        titles = dict(shortcuts.all_actions(self.is_teacher))
        # Осознанно снятое сочетание не должно вернуться из значений
        # по умолчанию.
        self.shortcut_map[action_id] = shortcuts.UNSET
        self._refresh_shortcut_list()
        announce(self.shortcut_list,
                 f"{titles.get(action_id, action_id)}: сочетание снято.")

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
        self._progress_step = 0
        self._downloading = True
        self.update_button.setEnabled(False)
        self.update_button.setText("Скачиваю…")
        self._set_update_status(
            "Скачиваю обновление. Это займёт несколько минут. Окно закроется "
            "само, а после замены программа откроется снова."
        )
        threading.Thread(target=self._download_worker, args=(url,), daemon=True).start()

    def _download_worker(self, url: str) -> None:
        try:
            path = updater.download(url, progress=self.sig_progress.emit)
        except Exception as exc:
            self.sig_download.emit({"error": _explain(exc)})
            return
        self.sig_download.emit({"path": str(path)})

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        percent = min(100, int(done * 100 / total))
        self.update_status.setText(f"Скачиваю обновление: {percent} процентов.")
        # Смена текста в строке состояния диктору не слышна: объявляем
        # каждые десять процентов словами, а не дёргаем его на каждом куске.
        step = percent // 10
        if step > self._progress_step:
            self._progress_step = step
            if step < 10:
                done_mb = done // (1024 * 1024)
                total_mb = max(1, total // (1024 * 1024))
                announce(
                    self.update_status,
                    f"Скачано {percent} процентов, {done_mb} из {total_mb} мегабайт.",
                )

    def _on_download_done(self, info: dict) -> None:
        self._downloading = False
        if info.get("error"):
            self.update_button.setEnabled(True)
            self.update_button.setText("Проверить обновления")
            self._set_update_status(
                f"Скачать обновление не удалось: {info['error']}. Нажмите "
                "«Проверить обновления» ещё раз: скачивание продолжится с того "
                "же места, а не начнётся заново."
            )
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
        player_cfg = cfg.setdefault("player", {})
        player_cfg["skip"] = int(self.skip_combo.currentData() or 5)
        player_cfg["stretch"] = str(self.stretch_combo.currentData()
                                    or effects.DEFAULT_ALGO)

        cfg["audio"]["driver"] = self._driver_value()
        cfg["audio"]["asio_device"] = str(self.asio_combo.currentData() or "")
        cfg["audio"]["asio_in_channels"] = int(self.asio_in_spin.value())
        cfg["audio"]["asio_out_channels"] = int(self.asio_out_spin.value())
        cfg["audio"]["device_buffer"] = int(
            self.device_buffer_combo.currentData() or 0)
        cfg["audio"]["device1"] = self.dev1_combo.currentData() or ""
        cfg["audio"]["device2"] = self.dev2_combo.currentData() or ""
        cfg["audio"]["usb"] = self.usb_combo.currentData() or ""
        cfg["audio"]["mode1"] = self.mode1_combo.currentData() or "ch1"
        cfg["audio"]["mode2"] = self.mode2_combo.currentData() or "ch1"
        cfg["audio"]["usb_mode"] = self.usb_mode_combo.currentData() or "ch1"
        cfg["audio"]["monitor1"] = self.monitor1_box.isChecked()
        cfg["audio"]["lowcut"] = self.lowcut_box.isChecked()
        cfg["audio"]["compressor"] = int(self.compressor_spin.value())
        cfg["audio"]["output"] = self.out_combo.currentData() or ""
        cfg["audio"]["midi_enabled"] = self.midi_box.isChecked()
        cfg["audio"]["midi_port"] = self.midi_combo.currentData() or ""
        cfg["audio"].update(self._midi_options())
        cfg["audio"]["show_all_drivers"] = self.all_drivers_box.isChecked()
        cfg["audio"]["buffer"] = str(self.buffer_combo.currentData() or "normal")
        cfg["audio"]["opus_bitrate"] = int(
            self.opus_bitrate_combo.currentData() or 96)
        cfg["audio"]["tx_delay_ms"] = int(self.tx_delay_spin.value())
        cfg["shortcuts"] = dict(self.shortcut_map)

        cfg["video"]["enabled"] = self.video_box.isChecked()
        cfg["video"]["camera"] = int(self.cam_combo.currentData() or 0)
        cfg["video"]["fps"] = int(self.fps_spin.value())
        cfg["video"]["quality"] = int(self.quality_spin.value())
        cfg["video"]["width"] = int(self.video_width_spin.value())
        cfg["video"]["height"] = int(self.video_height_spin.value())
        cfg["video"]["bitrate"] = int(
            self.video_bitrate_combo.currentData() or 0)
        cfg["video"]["show_remote"] = self.show_remote_box.isChecked()

        config.save(cfg)
        self.accept()


def _midi_ports() -> list[str]:
    try:
        from .midi_synth import list_midi_ports
        return list_midi_ports()
    except Exception:
        return []


def _engine_midi_options(audio_cfg: dict) -> dict:
    """Настройки синтезатора из файла настроек — аргументы configure().

    Один переводчик на запуск звука и на сохранение: иначе ключи файла
    и имена параметров синтезатора разошлись бы при первой правке.
    """
    def number(key, default):
        try:
            return float(audio_cfg.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    return {
        "wave": str(audio_cfg.get("midi_wave", "sine")),
        "dynamics": str(audio_cfg.get("midi_dynamics", "velocity")),
        "pedal": bool(audio_cfg.get("midi_pedal", False)),
        "attack_ms": number("midi_attack_ms", 6),
        "decay_ms": number("midi_decay_ms", 0),
        "sustain": number("midi_sustain", 100),
        "release_ms": number("midi_release_ms", 45),
    }


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")

# Сколько сообщений истории показывать за один раз. Вся переписка в окно
# не грузится: порция подставляется, когда курсор дошёл до верхнего
# сообщения, — на экране и в списке остаётся только нужное.
PAGE = 50


class ChatList(QListWidget):
    """Список сообщений чата.

    Обычное текстовое поле диктор читает только по последней строке, а
    список отдаёт каждое сообщение отдельным элементом — стрелки вверх и
    вниз ходят по всей переписке. Клавиша «Меню» на выбранном сообщении
    открывает правку и удаление, Delete удаляет сразу с подтверждением.
    """

    sig_delete = Signal()
    sig_edit = Signal()
    sig_open_link = Signal()
    sig_download = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("История чата")
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setWordWrap(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Delete:
            self.sig_delete.emit()
            event.accept()
            return
        # F2 — привычная клавиша переименования в Windows, тот же смысл.
        if event.key() == Qt.Key_F2:
            self.sig_edit.emit()
            event.accept()
            return
        # Enter на сообщении со ссылкой открывает её, на сообщении с файлом
        # скачивает файл. Так до ссылки можно добраться без мыши: стрелками
        # встать на сообщение и нажать Enter.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            item = self.currentItem()
            if item is not None:
                if find_link(str(item.data(Qt.UserRole + 3) or "")):
                    self.sig_open_link.emit()
                    event.accept()
                    return
                if item.data(Qt.UserRole + 4):
                    self.sig_download.emit()
                    event.accept()
                    return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        item = self.itemAt(event.pos())
        if item is None:
            super().contextMenuEvent(event)
            return
        self.setCurrentItem(item)
        menu = QMenu(self)
        menu.setAccessibleName("Действия с сообщением")
        open_link = None
        download = None
        if find_link(str(item.data(Qt.UserRole + 3) or "")):
            open_link = menu.addAction("Открыть ссылку")
        edit = menu.addAction("Редактировать")
        delete = menu.addAction("Удалить")
        if item.data(Qt.UserRole + 4):
            download = menu.addAction("Скачать файл")
        chosen = menu.exec(self.viewport().mapToGlobal(event.pos()))
        if chosen is None:
            return
        if chosen is open_link:
            self.sig_open_link.emit()
        elif chosen is edit:
            self.sig_edit.emit()
        elif chosen is delete:
            self.sig_delete.emit()
        elif chosen is download:
            self.sig_download.emit()


class VideoWindow(QWidget):
    """Отдельное окно с видео собеседника на весь экран.

    Открывается кнопкой «Развернуть видео». Картинка занимает весь экран
    поверх программы, а сама программа продолжает работать и показывает
    маленькое видео на главном окне. Escape или кнопка «Свернуть видео»
    закрывают окно — всё остаётся как было.
    """

    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(None)
        self.setWindowTitle("Мажорный лад — видео собеседника")
        self.setAccessibleName("Видео собеседника, отдельное окно")
        layout = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setAccessibleName("Видео собеседника")
        self.label.setMinimumSize(320, 240)
        layout.addWidget(self.label, 1)
        self.close_button = QPushButton("Свернуть видео")
        self.close_button.setAccessibleName("Свернуть видео")
        self.close_button.clicked.connect(self.close)
        layout.addWidget(self.close_button)

    def show_frame(self, pixmap) -> None:
        self.label.setPixmap(
            pixmap.scaled(self.label.size(), Qt.KeepAspectRatio,
                          Qt.SmoothTransformation))

    def clear_frame(self) -> None:
        self.label.clear()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)


class MainWindow(QMainWindow):
    sig_status = Signal(str)
    sig_chat = Signal(str, str, str)
    sig_chat_delete = Signal(str)
    sig_chat_edit = Signal(str, str)
    sig_peer = Signal(bool)
    sig_ready = Signal(str)
    sig_busy = Signal()
    sig_video = Signal(object)
    sig_join_request = Signal(object)
    sig_ended = Signal(str)
    sig_track_ready = Signal(str, object)
    sig_track_error = Signal(str)
    sig_effects_ready = Signal(object, int, float, int)
    sig_effects_error = Signal(str, int)
    sig_media_list = Signal(object)
    sig_media_saved = Signal(str)
    # Пришёл файл: кто прислал и где он лежит на диске.
    sig_file_message = Signal(str, str)
    sig_peer_video = Signal(bool)
    # Собеседник включил или выключил метроном: состояние, темп, доли и
    # признак «это состояние при входе в кабинет, а не нажатие кнопки».
    sig_peer_metro = Signal(bool, int, int, bool)
    # Порция прошлой переписки от преподавателя: список сообщений и
    # признак «раньше есть ещё».
    sig_history = Signal(object, bool)
    # В кабинет вошёл ученик, и в чат подставлена именно его переписка.
    sig_history_loaded = Signal(str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.is_teacher = cfg.get("role") == "teacher"
        self.engine = audio.AudioEngine(log=self._log_threadsafe)
        # Папка семплов создаётся сразу при запуске: файлы 001.wav …
        # 127.wav кладут туда заранее, и папка должна быть на месте раньше,
        # чем в настройках выберут «Семплер».
        try:
            from .midi_synth import sampler_dir

            sampler_dir()
        except Exception:
            pass
        self.reassembler = FrameReassembler(on_frame=self.sig_video.emit,
                                            log=self._log_threadsafe)
        self.engine.on_video = self.reassembler.feed
        self.server: Server | None = None
        self.client: Client | None = None
        self._join_queue: list = []
        self._join_dialog_open = False
        # История переписки. У преподавателя это чат с учеником, который
        # сейчас в кабинете: его показывает окно и постранично отдаёт
        # ученику. У каждого ученика свой файл в папке History. У ученика
        # здесь только загруженные с сервера сообщения.
        self._history: list[dict] = []
        # Чья переписка сейчас на экране: по этому имени сохраняются
        # правка и удаление сообщений, когда ученик уже отключился.
        self._history_student = ""
        self._history_more = False
        self._history_pending = False
        self._loading_history = False
        self.video_sender: VideoSender | None = None
        self.connected = False
        # Метроном ведёт только одна сторона: у кого он включён, тот и
        # щёлкает, второй слышит щелчки общим потоком и свой гасит.
        # _peer_metro_on — у собеседника метроном включён; _metro_held —
        # свой выключен именно ради этого, чтобы вернуть его после урока.
        self._peer_metro_on = False
        self._metro_held = False
        # Начало текущего урока: от него считается длительность, которая
        # потом ложится строкой в чат и в историю ученика.
        self._lesson_start: datetime | None = None
        self._lesson_student = ""
        # Ученик хочет быть в кабинете: пока это так, обрыв связи не
        # закрывает программу — она ждёт сеть и переподключается сама.
        self._want_connection = False
        self._waiting_network = False
        # Плеер: есть ли треки в Media и не идёт ли сейчас загрузка файла.
        self._tracks_available = False
        self._loading_track = False
        # Нужно ли включать звук, когда трек догрузится: при переключении
        # «Выше» и «Ниже» новый трек должен вести себя так же, как вёл
        # себя прежний — играл, стоял на паузе или ждал кнопки «Играть».
        self._load_play = True
        # Трек, который нажали выбрать, пока грузился прошлый: загрузится
        # сразу после него, чтобы быстрые нажатия не терялись.
        self._pending_track = ""
        self._pending_play = True
        # Подписи кнопок плеера переставляются только при смене состояния.
        self._player_button_state: tuple[bool, bool, int] = (False, False, 0)
        # Скорость и тон трека. Трек пересчитывается в фоне от исходного
        # звука, который хранится в _track_source: так несколько нажатий
        # подряд не накапливают искажения.
        self._speed = 1.0
        self._pitch = 0
        self._track_source = None
        self._effects_shown = False
        self._effects_busy = False
        # Номер пересчёта, который идёт сейчас: результат с чужим номером
        # не должен снимать признак занятости у свежего пересчёта.
        self._effects_job_gen = 0
        # Настройка, которая прямо сейчас звучит в плеере.
        self._effects_key: tuple[float, int] = (1.0, 0)
        self._effects_path = ""
        # Номер настройки. Результат, посчитанный для прежнего номера,
        # отбрасывается: пока он считался, скорость или тон уже сменили.
        self._effects_gen = 0
        # Готовые варианты трека: вернуться к уже посчитанной настройке
        # можно сразу, без повторного пересчёта всего файла.
        self._effects_cache: dict[tuple[float, int], object] = {}
        self._effects_cache_bytes = 0
        # Скорость, с которой сейчас звучит загруженный трек.
        self._effects_applied_speed = 1.0
        # Что сказать диктору о кнопках сброса при следующем отчёте.
        self._effects_hint = ""
        # Какую сторону регулируют ползунки: local — что слышу я,
        # peer — что слышит собеседник.
        self.volume_mode = str(cfg["audio"].get("volume_mode") or "local")
        if self.volume_mode not in ("local", "peer"):
            self.volume_mode = "local"
        self._mic_muted = bool(getattr(self.engine, "mic_muted", False))
        # Зажат ли сейчас правый Control ради короткой фразы.
        self._ptt_held = False
        # Сторож удержания и одноразовая запись кодов клавиши в журнал.
        self._ptt_timer: QTimer | None = None
        self._ptt_logged = False
        self._cam_on = False
        # Отдельное окно с видео на весь экран, пока оно открыто.
        self.video_window: VideoWindow | None = None
        self._shortcut_filter: _ShortcutFilter | None = None
        # Сочетание клавиш → действие. Собирается из настроек.
        self._shortcut_map: dict[str, str] = {}
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(lambda: config.save(self.cfg))
        # Громкости ученика пишутся отдельным, чуть более редким таймером:
        # ползунок во время движения шлёт десятки значений подряд.
        self._student_save_timer = QTimer(self)
        self._student_save_timer.setSingleShot(True)
        self._student_save_timer.setInterval(800)
        self._student_save_timer.timeout.connect(self._save_student_settings)
        # Повтор подключения, пока сети нет. Раз в пять секунд — редкий
        # опрос: человеку не нужна строка в чате на каждую попытку.
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(5000)
        self._retry_timer.timeout.connect(self._retry_connect)

        self.setWindowTitle(f"Мажорный лад {__version__}")
        self.resize(900, 680)
        self.setAccessibleName("Мажорный лад, главное окно")

        # Папка Sound и звуки по умолчанию. Свои файлы владелец кладёт туда
        # же — существующие не перезаписываются.
        sounds.ensure()

        self._build_ui()
        self._wire_signals()
        self._apply_volumes()
        self._apply_metronome()
        self._refresh_tracks()

        # Позиция трека и «трек закончился» читаются опросом: сам плеер
        # живёт в аудиопотоке и в интерфейс не стучится.
        self._player_timer = QTimer(self)
        self._player_timer.setInterval(400)
        self._player_timer.timeout.connect(self._tick_player)
        self._player_timer.start()

        # MIDI-клавиатура подключается и отключается молча: порты не шлют
        # событий, поэтому опрашиваем их раз в две секунды. Движок сам
        # решит, открывать ли порт — без клавиатуры опрос тихий.
        self._midi_timer = QTimer(self)
        self._midi_timer.setInterval(2000)
        self._midi_timer.timeout.connect(self._tick_midi)
        self._midi_timer.start()

        # Сочетания клавиш — из настроек, включая старые Control+P,
        # Control+A и Control+V. Фильтр стоит на приложении: в полях
        # ввода он уступает выделению, копированию и вставке.
        self._shortcut_filter = _ShortcutFilter(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._shortcut_filter)
        self._apply_shortcuts()

        if self.is_teacher:
            # Общий History.txt из версий до 0.2.5 переезжает в папку
            # History: переписка не теряется, а в папке программы
            # не остаётся файла, который больше ни к чему не относится.
            history.retire_legacy_file()
            # Чат при запуске пустой: переписка появляется, когда вошёл
            # ученик, — у каждого своя.
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

        # Через эту строку идут все объявления диктору: у неё в доступном
        # имени лежит сам текст, поэтому диктор читает сообщение целиком.
        self.announcer = Announcer(central)
        layout.addWidget(self.announcer)
        global _announcer
        _announcer = self.announcer

        # Видео собеседника
        self.video_label = QLabel()
        self.video_label.setMinimumHeight(240)
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setAccessibleName("Видео собеседника")
        self.video_label.setVisible(bool(self.cfg["video"].get("enabled")
                                         and self.cfg["video"].get("show_remote", True)))
        layout.addWidget(self.video_label, 1)
        # Рядом с видео — кнопка «на весь экран»: ученику смотреть урок
        # в маленьком квадратике неудобно. Свёрнутая картинка остаётся
        # на главном окне, программа работает как обычно.
        self.video_button = QPushButton("Развернуть видео")
        self.video_button.setAccessibleName("Развернуть видео на весь экран")
        self.video_button.setToolTip("Открыть видео отдельным окном на весь экран")
        self.video_button.clicked.connect(self._toggle_video_window)
        # Кнопка появляется вместе с первым кадром: пока видео нет,
        # разворачивать нечего.
        self.video_button.setVisible(False)
        layout.addWidget(self.video_button, 0, Qt.AlignLeft)

        # Громкости
        vol_group = QGroupBox("Громкость")
        vol_group.setAccessibleName("Регуляторы громкости")
        vol_layout = QVBoxLayout(vol_group)
        # Переключатель сторон: ползунки показывают либо мою громкость,
        # либо громкость, которую получает собеседник.
        # Обычная кнопка, не переключатель: диктор должен говорить только
        # «Моя громкость» или «Громкость ученика», без «отмечено».
        self.volume_mode_button = QPushButton()
        self.volume_mode_button.clicked.connect(self._toggle_volume_mode)
        vol_layout.addWidget(self.volume_mode_button)
        self.sliders: dict[str, QSlider] = {}
        self.slider_values: dict[str, QLabel] = {}
        self.slider_rows: dict[str, QWidget] = {}
        self.slider_titles: dict[str, str] = {}
        bus = self._volume_bus()
        for key, title in VOLUME_KEYS:
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
            slider.setValue(int(bus.get(key, 100)))
            slider.setTickPosition(QSlider.TicksBelow)
            slider.setTickInterval(10)
            slider.setAccessibleName(title)
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
        self._refresh_volume_mode()
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
        self.beats_spin = QSpinBox()
        self.beats_spin.setRange(1, 12)
        self.beats_spin.setAccessibleName("Ритм, долей в такте")
        metro_row.addWidget(label_for("Темп, ударов в минуту", self.bpm_spin))
        metro_row.addWidget(self.bpm_spin)
        metro_row.addSpacing(16)
        metro_row.addWidget(label_for("Ритм, долей в такте", self.beats_spin))
        metro_row.addWidget(self.beats_spin)
        metro_row.addStretch(1)
        metro_layout.addWidget(self.metro_row)
        layout.addWidget(metro_group)

        # Плеер
        player_group = QGroupBox("Плеер")
        player_group.setAccessibleName("Плеер")
        player_layout = QVBoxLayout(player_group)

        track_row = QHBoxLayout()
        self.track_combo = QComboBox()
        self.track_combo.setAccessibleName("Трек")
        track_row.addWidget(label_for("Трек", self.track_combo))
        track_row.addWidget(self.track_combo, 1)
        self.add_track_button = QPushButton("Добавить трек")
        self.add_track_button.setAccessibleName("Добавить трек")
        track_row.addWidget(self.add_track_button)
        # Ученик забирает треки из кабинета преподавателя в свою Media.
        # Кнопка нужна обеим сторонам: ученик берёт трек из Media
        # преподавателя, преподаватель — из Media ученика.
        # Текст один и тот же в обеих версиях; от кого именно трек, диктор
        # читает из доступного имени — по кнопке сразу понятно.
        import_label = "Импорт трека"
        import_name = ("Импорт трека от ученика" if self.is_teacher
                       else "Импорт трека от преподавателя")
        self.import_button = QPushButton(import_label)
        self.import_button.setAccessibleName(import_name)
        track_row.addWidget(self.import_button)
        player_layout.addLayout(track_row)

        self.player_buttons_row = QWidget()
        buttons_row = QHBoxLayout(self.player_buttons_row)
        buttons_row.setContentsMargins(0, 0, 0, 0)
        # Одна главная кнопка: «Играть» — «Стоп» — «Продолжить». Пауза
        # вынесена отдельно: распознать двойное нажатие можно только с
        # задержкой в четверть секунды, и одиночное нажатие тогда тоже
        # срабатывало бы с этой задержкой.
        self.play_button = QPushButton("Играть")
        self.play_button.setAccessibleName("Играть трек")
        self.pause_button = QPushButton("Пауза")
        self.pause_button.setAccessibleName("Пауза")
        self.pause_button.setEnabled(False)
        # Подписи «Выше» и «Ниже» короткие, как просил владелец; диктор
        # говорит по доступному имени, что именно переключается.
        self.prev_button = QPushButton("Выше")
        self.prev_button.setAccessibleName("Предыдущий трек")
        self.next_button = QPushButton("Ниже")
        self.next_button.setAccessibleName("Следующий трек")
        self.back_button = QPushButton("Назад")
        self.back_button.setAccessibleName("Назад")
        self.forward_button = QPushButton("Вперёд")
        self.forward_button.setAccessibleName("Вперёд")
        self.effects_button = QPushButton("Скорость и тон")
        self.effects_button.setAccessibleName("Скорость и тон")
        for button in (self.play_button, self.pause_button,
                       self.prev_button, self.next_button,
                       self.back_button, self.forward_button,
                       self.effects_button):
            buttons_row.addWidget(button)
        buttons_row.addStretch(1)
        player_layout.addWidget(self.player_buttons_row)

        # Кнопки эффектов прячутся за кнопкой «Скорость и тон»: в обычной работе
        # они не нужны и только удлиняют обход. Внизу — сбросы, они
        # появляются, только когда скорость или тон действительно изменены.
        self.effects_row = QWidget()
        effects_layout = QVBoxLayout(self.effects_row)
        effects_layout.setContentsMargins(0, 0, 0, 0)
        change_row = QHBoxLayout()
        self.slow_button = QPushButton("Замедлить на 10%")
        self.slow_button.setAccessibleName("Замедлить на 10%")
        self.fast_button = QPushButton("Убыстрить на 10%")
        self.fast_button.setAccessibleName("Убыстрить на 10%")
        self.lower_button = QPushButton("Опустить на 1 полутон")
        self.lower_button.setAccessibleName("Опустить на 1 полутон")
        self.raise_button = QPushButton("Поднять на 1 полутон")
        self.raise_button.setAccessibleName("Поднять на 1 полутон")
        for button in (self.slow_button, self.fast_button,
                       self.lower_button, self.raise_button):
            change_row.addWidget(button)
        change_row.addStretch(1)
        reset_row = QHBoxLayout()
        self.speed_reset_button = QPushButton("Сброс скорости")
        self.speed_reset_button.setAccessibleName("Сброс скорости")
        self.pitch_reset_button = QPushButton("Сброс тона")
        self.pitch_reset_button.setAccessibleName("Сброс тона")
        self.speed_reset_button.setVisible(False)
        self.pitch_reset_button.setVisible(False)
        reset_row.addWidget(self.speed_reset_button)
        reset_row.addWidget(self.pitch_reset_button)
        reset_row.addStretch(1)
        effects_layout.addLayout(change_row)
        effects_layout.addLayout(reset_row)
        player_layout.addWidget(self.effects_row)
        self.effects_row.setVisible(False)

        self.track_position_label = QLabel("")
        self.track_position_label.setAccessibleName("Позиция трека")
        player_layout.addWidget(self.track_position_label)
        self.player_buttons_row.setVisible(False)
        self.track_position_label.setVisible(False)
        layout.addWidget(player_group)

        # Имя и подключение (для ученика)
        self.name_row = QWidget()
        name_layout = QHBoxLayout(self.name_row)
        name_layout.setContentsMargins(0, 0, 0, 0)
        self.name_edit = QLineEdit(self.cfg.get("name", ""))
        self.name_edit.setAccessibleName("Ваше имя")
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
        self.chat_list = ChatList()
        chat_layout.addWidget(self.chat_list, 1)

        input_row = QHBoxLayout()
        self.message_edit = QLineEdit()
        self.message_edit.setAccessibleName("Сообщение")
        self.send_button = QPushButton("Отправить")
        self.send_button.setAccessibleName("Отправить сообщение")
        self.file_button = QPushButton("Отправить файл")
        self.file_button.setAccessibleName("Отправить файл собеседнику")
        self.download_button = QPushButton("Скачать")
        self.download_button.setAccessibleName(
            "Скачать файл из выбранного сообщения в папку Media")
        self.download_button.setToolTip(
            "Сохраняет файл из выбранного сообщения в папку Media")
        input_row.addWidget(label_for("Сообщение", self.message_edit))
        input_row.addWidget(self.message_edit, 1)
        input_row.addWidget(self.send_button)
        input_row.addWidget(self.file_button)
        input_row.addWidget(self.download_button)
        chat_layout.addLayout(input_row)
        layout.addWidget(chat_group, 2)

        # Кнопки внизу
        bottom = QHBoxLayout()
        self.settings_button = QPushButton("Настройки (Control+P)")
        self.settings_button.setAccessibleName("Настройки, Control P")
        self.connect_button = QPushButton("Открыть кабинет" if self.is_teacher else "Подключиться")
        self.connect_button.setAccessibleName(self.connect_button.text())
        self.mic_button = QPushButton("Микрофон включён")
        self.mic_button.setAccessibleName("Микрофон включён")
        self.cam_button = QPushButton("Камера выключена")
        self.cam_button.setAccessibleName("Камера выключена")
        bottom.addWidget(self.settings_button)
        bottom.addWidget(self.connect_button)
        bottom.addWidget(self.mic_button)
        bottom.addWidget(self.cam_button)
        bottom.addStretch(1)
        layout.addLayout(bottom)

        self.setCentralWidget(central)
        self.message_edit.returnPressed.connect(self._send_message)
        self.send_button.clicked.connect(self._send_message)
        self.file_button.clicked.connect(self._send_file)
        self.download_button.clicked.connect(lambda: self._download_chat_file())
        self.settings_button.clicked.connect(self.open_settings)
        self.join_button.clicked.connect(self._join)
        self.connect_button.clicked.connect(self._on_connect_button)
        self.metro_button.toggled.connect(self._on_metronome_toggle)
        self.bpm_spin.valueChanged.connect(self._on_metronome_value)
        self.beats_spin.valueChanged.connect(self._on_metronome_value)
        self.add_track_button.clicked.connect(self._add_track)
        self.import_button.clicked.connect(self._import_audio)
        self.mic_button.clicked.connect(self._toggle_mic)
        self.cam_button.clicked.connect(self._toggle_camera)
        self.track_combo.currentIndexChanged.connect(self._on_track_changed)
        self.play_button.clicked.connect(self._toggle_play)
        self.pause_button.clicked.connect(self._pause_play)
        self.prev_button.clicked.connect(lambda: self._step_track(-1))
        self.next_button.clicked.connect(lambda: self._step_track(1))
        self.back_button.clicked.connect(lambda: self._seek(-1))
        self.forward_button.clicked.connect(lambda: self._seek(1))
        self.effects_button.clicked.connect(self._toggle_effects)
        self.slow_button.clicked.connect(lambda: self._change_speed(-1))
        self.fast_button.clicked.connect(lambda: self._change_speed(1))
        self.lower_button.clicked.connect(lambda: self._change_pitch(-1))
        self.raise_button.clicked.connect(lambda: self._change_pitch(1))
        self.speed_reset_button.clicked.connect(self._reset_speed)
        self.pitch_reset_button.clicked.connect(self._reset_pitch)

    def _wire_signals(self) -> None:
        self.sig_status.connect(self._set_status)
        self.sig_track_ready.connect(self._on_track_ready)
        self.sig_track_error.connect(self._on_track_error)
        self.sig_effects_ready.connect(self._on_effects_ready)
        self.sig_effects_error.connect(self._on_effects_error)
        self.sig_chat.connect(self._append_chat)
        self.sig_chat_delete.connect(self._on_chat_delete)
        self.sig_chat_edit.connect(self._on_chat_edit)
        self.sig_history.connect(self._on_history)
        self.sig_history_loaded.connect(self._on_history_loaded)
        # Поднялись стрелкой на самое верхнее сообщение — догружаем
        # предыдущую порцию переписки.
        self.chat_list.currentRowChanged.connect(self._on_chat_row_changed)
        self.chat_list.sig_delete.connect(self._delete_chat_message)
        self.chat_list.sig_edit.connect(self._edit_chat_message)
        self.chat_list.sig_open_link.connect(self._open_chat_link)
        self.chat_list.sig_download.connect(self._download_chat_file)
        self.sig_file_message.connect(self._on_file_message)
        self.sig_peer.connect(self._on_peer)
        self.sig_ready.connect(self._on_ready)
        self.sig_busy.connect(self._on_busy)
        self.sig_video.connect(self._on_video_frame)
        self.sig_join_request.connect(self._on_join_request)
        self.sig_ended.connect(self._on_ended)
        self.sig_media_list.connect(self._on_media_list)
        self.sig_media_saved.connect(self._on_media_saved)
        self.sig_peer_video.connect(self._on_peer_video)
        self.sig_peer_metro.connect(self._on_peer_metro)

    # -- запуск ролей -------------------------------------------------------

    def _start_teacher(self) -> None:
        name = self.cfg.get("name") or "Преподаватель"
        self.server = Server(
            name,
            on_status=self.sig_status.emit,
            on_chat=self.sig_chat.emit,
            on_chat_delete=self.sig_chat_delete.emit,
            on_chat_edit=self.sig_chat_edit.emit,
            on_file=self.sig_file_message.emit,
            on_media_list=self.sig_media_list.emit,
            on_media_saved=self.sig_media_saved.emit,
            on_peer=self.sig_peer.emit,
            on_join_request=self.sig_join_request.emit,
            on_video_state=self.sig_peer_video.emit,
            on_metro_state=self.sig_peer_metro.emit,
            on_history_loaded=self.sig_history_loaded.emit,
            chat_history=self._history,
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

    def _connect(self, silent: bool = False) -> None:
        """Подключается к кабинету преподавателя.

        silent — повторная попытка, пока нет сети: без окна и без
        «Подключаюсь» в статусе, чтобы ожидание не шумело.
        """
        host = self.cfg.get("server_ip", "").strip()
        if not host:
            self._stop_waiting()
            self._set_status("Не задан адрес преподавателя. Откройте настройки, Control+P.")
            if not silent:
                QMessageBox.information(
                    self, "Нужен адрес",
                    "В настройках, раздел «Общее», укажите адрес преподавателя. "
                    "Он выдаёт этот адрес ученикам."
                )
            return
        name = self.cfg.get("name", "").strip() or self.name_edit.text().strip()
        if not name:
            self._stop_waiting()
            self._set_status("Сначала введите имя.")
            return
        self._want_connection = True
        self.client = Client(
            name, host,
            port=int(self.cfg.get("server_port", 45800)),
            on_status=self.sig_status.emit,
            on_chat=self.sig_chat.emit,
            on_chat_delete=self.sig_chat_delete.emit,
            on_chat_edit=self.sig_chat_edit.emit,
            on_file=self.sig_file_message.emit,
            on_ready=self.sig_ready.emit,
            on_busy=self.sig_busy.emit,
            on_peer=self.sig_peer.emit,
            on_ended=self.sig_ended.emit,
            on_media_list=self.sig_media_list.emit,
            on_media_saved=self.sig_media_saved.emit,
            on_video_state=self.sig_peer_video.emit,
            on_metro_state=self.sig_peer_metro.emit,
            on_history=self.sig_history.emit,
        )
        if not silent:
            self._set_status(f"Подключаюсь к {host}…")
        # connect() может ждать до шести секунд — не держим интерфейс.
        threading.Thread(target=self.client.connect, daemon=True).start()

    def _retry_connect(self) -> None:
        """Очередная попытка подключиться, пока сети не было."""
        if not self._waiting_network or self.client is not None:
            return
        self._connect(silent=True)

    def _enter_waiting(self) -> None:
        """Связь пропала: программа не закрывается, а ждёт её возвращения."""
        first = not self._waiting_network
        self._waiting_network = True
        self.client = None
        self.connected = False
        self._stop_video()
        self._close_video_window()
        self._cam_on = False
        self._update_cam_button()
        # Кнопка остаётся «Отключиться»: она теперь отменяет ожидание.
        self.connect_button.setText("Отключиться")
        self.connect_button.setAccessibleName("Отключиться")
        text = ("Связь с преподавателем прервалась. Жду восстановления "
                "интернета и подключусь заново сам. Программа остаётся "
                "открытой; «Отключиться» завершает урок.")
        self._set_status(text)
        if first:
            sounds.play("disconnect")
            self._append_chat("система", text)
            announce(self.status_label, text)
        self._retry_timer.start()

    def _stop_waiting(self) -> None:
        self._waiting_network = False
        self._retry_timer.stop()

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
                # Кабинет закрывают посреди урока: фиксируем длительность,
                # пока история ученика ещё открыта.
                self._lesson_finish()
                self.server.stop()
                self.server = None
                self.engine.stop()
                self._stop_video()
                self.connected = False
                self.connect_button.setText("Открыть кабинет")
                self.connect_button.setAccessibleName("Открыть кабинет")
                self._set_status("Кабинет закрыт.")
            else:
                self._start_teacher()
        else:
            if self.client is not None or self._waiting_network:
                # «Отключиться» во время ожидания сети тоже закрывает урок.
                self._want_connection = False
                if self._waiting_network:
                    self._stop_waiting()
                    if self._lesson_start is not None:
                        length = _duration_word(
                            (datetime.now()
                             - self._lesson_start).total_seconds())
                        self._lesson_start = None
                        self._lesson_student = ""
                        self._append_chat(
                            "система",
                            f"Урок закончен. Длительность: {length}.")
                if self.client is not None:
                    self.client.stop()
                self.client = None
                self.engine.stop()
                self.connected = False
                self._stop_video()
                self._cam_on = False
                self._update_cam_button()
                self.connect_button.setText("Подключиться")
                self.connect_button.setAccessibleName("Подключиться")
                # После отключения местный звук остаётся: метроном и MIDI
                # нужны и для самостоятельной работы.
                self._start_audio(local_only=True)
                self._set_status("Отключено.")
            else:
                self._connect()

    # -- длительность урока --------------------------------------------------

    def _lesson_begin(self, student: str = "") -> None:
        """Запоминает начало урока. Повторный сигнал время не сдвигает."""
        if self._lesson_start is not None:
            return
        self._lesson_start = datetime.now()
        self._lesson_student = str(student or "")

    def _lesson_finish(self) -> None:
        """Пишет в чат и в историю, что урок закончился и сколько длился."""
        started = self._lesson_start
        if started is None:
            return
        # Громкости уходят в файл ученика до того, как имя урока сбросится:
        # следующий урок начнётся с этих значений.
        self._save_student_settings()
        self._lesson_start = None
        length = _duration_word((datetime.now() - started).total_seconds())
        name = self._lesson_student or self._history_student_name()
        self._lesson_student = ""
        if name:
            text = (f"Урок закончен. Ученик: {name}. "
                    f"Длительность: {length}.")
        else:
            text = f"Урок закончен. Длительность: {length}."
        # Номер постоянный: заметку видно и в истории ученика, и после
        # перезапуска программы.
        self._append_chat("система", text,
                          mid=f"note-{uuid.uuid4().hex}", keep=True, owner=name)
        sounds.play("disconnect")

    def _on_ready(self, teacher: str) -> None:
        restored = self._waiting_network
        if restored:
            self._stop_waiting()
        self._set_status(f"Соединение установлено: {teacher}.")
        if not self.is_teacher:
            self._lesson_begin(teacher)
            sounds.play("connect")
        if self.client is not None and not self.is_teacher:
            # Переписка прошлых занятий приходит от преподавателя заново.
            # Строки от прошлого подключения надо убрать: иначе новая
            # порция встала бы над ними и порядок перепутался.
            self.chat_list.clear()
            self._history_more = True
            self._history_pending = False
        self._append_chat("система", f"Соединение установлено. {teacher} на связи.")
        self.name_row.setVisible(False)
        self.connect_button.setText("Отключиться")
        self.connect_button.setAccessibleName("Отключиться")
        self._start_audio()
        # Преподаватель должен знать, не остался ли метроном включённым с
        # прошлого урока: метроном ведёт только одна сторона.
        if self.client is not None and not self.is_teacher:
            self._send_metro_state(self.metro_button.isChecked(), hello=True)
        # Сразу подтягиваем последние сообщения прошлых занятий — так ученик
        # открывает чат и видит, о чём говорили в прошлый раз.
        if self.client is not None and not self.is_teacher:
            self._history_more = True
            self._history_pending = False
            self._request_history("")
        if restored:
            text = "Связь восстановлена. Урок продолжается."
            self._set_status(text)
            announce(self.status_label, text)

    def _on_busy(self) -> None:
        # Кабинет занят: программа уже спросила преподавателя. Окно не
        # открываем — ответ может прийти и через минуту, а модальное окно
        # держало бы ученика и мешало ему работать.
        text = "Идёт урок с другим учеником. Спросил преподавателя, жду ответа."
        sounds.play("busy")
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
        sounds.play("busy")
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
            "refused": "Подождите, кабинет пока закрыт. Преподаватель скоро его откроет.",
            "timeout": "Преподаватель не отвечает. Проверьте адрес в настройках, "
                       "Control+P.",
            "network": "Нет связи с сетью или интернетом. Жду восстановления "
                       "подключения.",
        }
        text = texts.get(reason, texts["lost"])
        # Обрыв связи посреди урока: не сдаёмся и не закрываем окно —
        # ждём сеть и переподключаемся сами. Остальные причины (закрытый
        # кабинет, отказ, молчание преподавателя) остаются как были.
        if self._waiting_network or (reason in ("lost", "network")
                                     and self._want_connection):
            self._enter_waiting()
            return
        self._want_connection = False
        if self._lesson_start is not None:
            # Ученик видит, сколько шёл урок, — та же строка уходит
            # в переписку преподавателя.
            length = _duration_word(
                (datetime.now() - self._lesson_start).total_seconds())
            self._lesson_start = None
            self._lesson_student = ""
            text = f"{text} Урок длился {length}."
        # «Кабинет закрыт» — не разрыв урока, а первая попытка подключиться:
        # звук отключения там только пугает.
        if reason not in ("refused", "timeout"):
            sounds.play("disconnect")
        self.client = None
        self.connected = False
        self._stop_video()
        self._close_video_window()
        self._cam_on = False
        self._update_cam_button()
        self.connect_button.setText("Подключиться")
        self.connect_button.setAccessibleName("Подключиться")
        self._set_status(text)
        self._append_chat("система", text)
        announce(self.status_label, text)

    def _on_peer(self, present: bool) -> None:
        self.connected = bool(present)
        if not present:
            self._stop_video()
            self._close_video_window()
            self.engine.student.clear()
            self._peer_metro_on = False
            if self._metro_held:
                # Урок кончился — возвращаем свой метроном, который гасили
                # ради щелчков собеседника.
                self._metro_hold(False)
            if self.is_teacher:
                # Кабинет остаётся открытым: местный звук, метроном и MIDI
                # продолжают работать, пока ждём следующего ученика.
                # Если кабинет уже закрыт, это запоздалый сигнал от
                # закрытого соединения — он не должен перебивать «Кабинет
                # закрыт».
                if self.server is not None:
                    self._lesson_finish()
                    self._set_status("Ученик отключился. Кабинет свободен.")
            else:
                self.engine.stop()
                self._start_audio(local_only=True)
            return
        if self.is_teacher:
            # Если прошлый урок ещё не закрыт отдельным сигналом (ученика
            # заменили), фиксируем его до начала нового.
            self._lesson_finish()
            name = getattr(self.server, "active_name", "") if self.server else ""
            restored = self._restore_student_settings(name) if name else False
            self._lesson_begin(name)
            sounds.play("connect")
            text = "Ученик подключился."
            if restored:
                text += " Громкости с прошлого урока восстановлены."
            self._set_status(text)
            self._append_chat("система", "Ученик подключился.")
            if self.engine.running:
                self._start_video()
            else:
                self._start_audio()
            # Сообщаем ученику, кто ведёт метроном, — у него мог остаться
            # включённым с прошлого урока.
            self._send_metro_state(self.metro_button.isChecked(), hello=True)

    def _start_audio(self, local_only: bool = False) -> bool:
        """Открывает звуковой поток.

        local_only — только свои наушники, без отправки собеседнику: так
        метроном и MIDI-клавиатура работают до подключения и без сети.
        """
        cfg_audio = self.cfg["audio"]
        audio.set_buffer_preset(str(cfg_audio.get("buffer", "normal")))
        # Ручная компенсация нужна движку до старта: авто он посчитает сам,
        # когда откроет устройства.
        self.engine.tx_delay_ms = int(cfg_audio.get("tx_delay_ms", 0) or 0)
        self.engine.set_opus_bitrate(
            int(cfg_audio.get("opus_bitrate", 96) or 96))
        devices = {
            "in1": cfg_audio.get("device1", ""),
            "in2": cfg_audio.get("device2", ""),
            "usb": cfg_audio.get("usb", ""),
        }
        modes = {
            "in1": cfg_audio.get("mode1", "ch1"),
            "in2": cfg_audio.get("mode2", "ch1"),
            "usb": cfg_audio.get("usb_mode", "ch1"),
        }
        is_server = self.is_teacher
        server_addr = None
        if not is_server and not local_only:
            host = self.cfg.get("server_ip", "").strip()
            if host:
                server_addr = (host, audio.UDP_PORT)
        input_fx = {
            "monitor": bool(cfg_audio.get("monitor1", False)),
            "lowcut": bool(cfg_audio.get("lowcut", False)),
            "compressor": int(cfg_audio.get("compressor", 0) or 0),
        }
        ok, message = self.engine.start(
            is_server=is_server,
            server_addr=server_addr,
            devices=devices,
            modes=modes,
            output=cfg_audio.get("output", ""),
            midi_enabled=bool(cfg_audio.get("midi_enabled", False)),
            midi_port=cfg_audio.get("midi_port", ""),
            midi_options=_engine_midi_options(cfg_audio),
            driver=str(cfg_audio.get("driver", "wasapi")),
            asio_device=str(cfg_audio.get("asio_device", "")),
            asio_in=int(cfg_audio.get("asio_in_channels", 2) or 2),
            asio_out=int(cfg_audio.get("asio_out_channels", 2) or 2),
            input_fx=input_fx,
            device_buffer=int(cfg_audio.get("device_buffer", 0) or 0),
        )
        self._set_status(message)
        if ok:
            self._apply_volumes()
            self._apply_metronome()
            if self.connected:
                self._start_video()
        return ok

    def _start_video(self, force: bool = False) -> bool:
        if self.video_sender is not None:
            return True
        if not force and not self.cfg["video"].get("enabled"):
            return False
        self.video_label.setVisible(bool(self.cfg["video"].get("show_remote", True)))
        sender = VideoSender(
            camera=int(self.cfg["video"].get("camera", 0)),
            fps=int(self.cfg["video"].get("fps", 15)),
            quality=int(self.cfg["video"].get("quality", 60)),
            width=int(self.cfg["video"].get("width", 640)),
            height=int(self.cfg["video"].get("height", 480)),
            bitrate=int(self.cfg["video"].get("bitrate", 0)),
            send_fn=self.engine.send_video_packet,
            log=self._log_threadsafe,
        )
        ok, message = sender.start()
        if not ok:
            self._log_threadsafe(message)
            return False
        self.video_sender = sender
        self._cam_on = True
        self._update_cam_button()
        self._send_video_state(True)
        return True

    def _stop_video(self) -> None:
        had_camera = self.video_sender is not None
        if self.video_sender is not None:
            self.video_sender.stop()
            self.video_sender = None
        self.video_label.clear()
        self._cam_on = False
        self._update_cam_button()
        if had_camera:
            self._send_video_state(False)

    def _send_video_state(self, on: bool) -> None:
        """Сказать собеседнику, что камера включилась или погасла."""
        try:
            if self.is_teacher and self.server is not None:
                self.server.send_video_state(on)
            elif self.client is not None:
                self.client.send_video_state(on)
        except Exception:
            pass

    def _send_metro_state(self, on: bool, hello: bool = False) -> None:
        """Сказать собеседнику, кто сейчас ведёт метроном."""
        try:
            if self.is_teacher and self.server is not None:
                self.server.send_metro_state(on, self.bpm_spin.value(),
                                             self.beats_spin.value(), hello)
            elif self.client is not None:
                self.client.send_metro_state(on, self.bpm_spin.value(),
                                             self.beats_spin.value(), hello)
        except Exception:
            pass

    def _metro_hold(self, hold: bool) -> None:
        """Выключает свой метроном (или возвращает) без нажатия кнопки.

        Пока щелчки идут от собеседника, свой метроном молчит: два щелчка
        вразнобой сбивают. Настройка сохраняется, чтобы после обрыва урока
        метроном не включился сам в новой игре.
        """
        self._metro_held = bool(hold)
        cfg_metro = self.cfg.setdefault("metronome", {})
        cfg_metro["enabled"] = not hold
        self.metro_button.blockSignals(True)
        self.metro_button.setChecked(not hold)
        self.metro_button.blockSignals(False)
        self._update_metro_button()
        self.engine.set_metronome(not hold, self.bpm_spin.value(),
                                  self.beats_spin.value())
        config.save(self.cfg)
        self._refresh_volume_visibility()

    def _on_peer_metro(self, on: bool, bpm: int, beats: int,
                       hello: bool = False) -> None:
        """Собеседник включил или выключил метроном.

        Щелчки рождаются только у одной стороны и идут второй в общем
        потоке с голосом. Получив «включил», свой метроном гасим.
        """
        on = bool(on)
        if not on:
            self._peer_metro_on = False
            if self._metro_held:
                self._metro_hold(False)
                text = ("Собеседник выключил метроном. "
                        "Свой метроном снова включён.")
                self._set_status(text)
                announce(self.status_label, text)
            return
        if hello and self.is_teacher and self.metro_button.isChecked():
            # Оба вошли в кабинет с метрономом, оставшимся с прошлого
            # урока. Метроном ведёт преподаватель; ученик получит наше
            # «включён» и свой погасит.
            self._send_metro_state(True)
            return
        first = not self._peer_metro_on
        self._peer_metro_on = True
        if not self.metro_button.isChecked():
            if first:
                text = (f"У собеседника включён метроном: {bpm} "
                        f"{_plural(bpm, 'удар', 'удара', 'ударов')} в минуту, "
                        f"{beats} {_plural(beats, 'доля', 'доли', 'долей')} "
                        "в такте.")
                self._set_status(text)
                announce(self.status_label, text)
            return
        self._metro_hold(True)
        text = (f"У собеседника включён метроном: {bpm} "
                f"{_plural(bpm, 'удар', 'удара', 'ударов')} в минуту, "
                f"{beats} {_plural(beats, 'доля', 'доли', 'долей')} в такте. "
                "Свой метроном выключен, чтобы не сбивать.")
        self._set_status(text)
        announce(self.status_label, text)

    def _update_cam_button(self) -> None:
        # На кнопке — состояние, а не действие: диктор, попав на неё,
        # сразу говорит, работает камера или нет. Что сделает нажатие,
        # объясняет описание.
        if self._cam_on:
            text, action = "Камера включена", "Нажмите, чтобы выключить камеру"
        else:
            text, action = "Камера выключена", "Нажмите, чтобы включить камеру"
        hint = self._hint("camera")
        self.cam_button.setText(text)
        self.cam_button.setAccessibleName(text)
        self.cam_button.setAccessibleDescription(
            action + (f". Сочетание клавиш{hint}" if hint else ""))

    def _toggle_camera(self) -> None:
        if self._cam_on or self.video_sender is not None:
            self._stop_video()
            self._set_status("Камера выключена.")
            announce(self.cam_button, "Камера выключена")
            return
        if self._start_video(force=True):
            self._set_status("Камера включена.")
            announce(self.cam_button, "Камера включена")
        else:
            self._set_status(
                "Камера не запустилась. Проверьте камеру в настройках видео.")
            announce(self.cam_button,
                     "Камера не запустилась. Проверьте камеру в настройках")

    def _on_peer_video(self, on: bool) -> None:
        """Собеседник выключил камеру — убираем застывший кадр."""
        if not on:
            self.video_label.clear()
            self.video_button.setVisible(False)
            self._close_video_window()

    def _close_video_window(self) -> None:
        """Закрывает развёрнутое видео, если оно открыто."""
        if self.video_window is not None:
            self.video_window.close()

    def _toggle_video_window(self) -> None:
        """Открывает видео отдельным окном на весь экран или закрывает его."""
        if self.video_window is not None:
            self.video_window.close()
            return
        window = VideoWindow()
        window.closed.connect(self._on_video_window_closed)
        self.video_window = window
        # Уже показанный кадр виден сразу, не ждём следующего.
        pixmap = self.video_label.pixmap()
        if pixmap is not None and not pixmap.isNull():
            window.label.setPixmap(pixmap)
        window.showFullScreen()
        window.raise_()
        window.activateWindow()
        window.close_button.setFocus()
        self.video_button.setText("Свернуть видео")
        self.video_button.setAccessibleName("Свернуть видео")
        self._set_status(
            "Видео открыто на весь экран. Escape или кнопка «Свернуть видео» "
            "вернут маленькую картинку.")

    def _on_video_window_closed(self) -> None:
        self.video_window = None
        self.video_button.setText("Развернуть видео")
        self.video_button.setAccessibleName("Развернуть видео на весь экран")
        self._set_status("Видео свёрнуто. Программа работает как обычно.")

    # -- микрофон -----------------------------------------------------------

    def _update_mic_button(self) -> None:
        # Так же, как у камеры: на кнопке состояние, действие — в описании.
        if self._mic_muted:
            text = "Микрофон выключен"
            action = ("Нажмите, чтобы включить микрофон. Для короткой фразы "
                      "можно просто удерживать правый Control: нажал — "
                      "говоришь, отпустил — микрофон снова выключен")
        else:
            text, action = "Микрофон включён", "Нажмите, чтобы выключить микрофон"
        hint = self._hint("mic")
        self.mic_button.setText(text)
        self.mic_button.setAccessibleName(text)
        self.mic_button.setAccessibleDescription(
            action + (f". Сочетание клавиш{hint}" if hint else ""))

    def _toggle_mic(self) -> None:
        self._mic_muted = not self._mic_muted
        self._ptt_held = False
        self._ptt_watch_stop()
        self.engine.set_mic_muted(self._mic_muted)
        self._update_mic_button()
        text = "Микрофон выключен" if self._mic_muted else "Микрофон включён"
        self._set_status(text + ".")
        announce(self.mic_button, text)

    # -- микрофон на зажатом правом Control ----------------------------------

    @staticmethod
    def _is_right_ctrl(event) -> bool:
        """Правое Control. В Qt обе клавиши приходят одним Key_Control,
        сторону различаем по системе: спрашиваем у Windows, зажат ли именно
        правый Control. Коды из самого события — запасной путь, если
        система не ответила."""
        if event.key() != Qt.Key.Key_Control:
            return False
        if _right_ctrl_down():
            return True
        try:
            if int(event.nativeVirtualKey()) == VK_RCONTROL:
                return True
            return int(event.nativeScanCode()) == 0x11D
        except Exception:
            return False

    def _ptt_watch_start(self) -> None:
        """Сторож на время удержания: если отпускание перехватило другое
        окно, микрофон всё равно выключится."""
        if _user32() is None:
            return
        if self._ptt_timer is None:
            self._ptt_timer = QTimer(self)
            self._ptt_timer.setInterval(120)
            self._ptt_timer.timeout.connect(self._ptt_watch)
        self._ptt_timer.start()

    def _ptt_watch_stop(self) -> None:
        if self._ptt_timer is not None:
            self._ptt_timer.stop()

    def _ptt_watch(self) -> None:
        if not self._ptt_held:
            self._ptt_watch_stop()
            return
        if not _right_ctrl_down():
            self._ptt_off()

    def _ptt_press(self, event) -> bool:
        """Правый Control зажат: если микрофон выключен, включаем, пока
        держат. Если он и так включён — ничего не делаем."""
        if event.isAutoRepeat() or not self.isActiveWindow():
            return False
        if not self._ptt_logged and event.key() == Qt.Key.Key_Control:
            # Одна строка в журнал: по ней видно, что именно приходит от
            # клавиатуры, если у человека снова не сработает.
            self._ptt_logged = True
            try:
                self.engine.log(
                    "Правый Control: скан-код "
                    f"{int(event.nativeScanCode())}, виртуальная клавиша "
                    f"{int(event.nativeVirtualKey())}, зажата правой: "
                    f"{'да' if _right_ctrl_down() else 'нет'}.")
            except Exception:
                pass
        if not self._is_right_ctrl(event):
            return False
        if not self._mic_muted or self._ptt_held:
            return False
        self._ptt_held = True
        # Микрофон считается включённым, пока держат Control: кнопка
        # и диктор должны говорить о том, что слышит собеседник.
        self._mic_muted = False
        self.engine.set_mic_muted(False)
        self._update_mic_button()
        self._set_status("Микрофон включён. Отпустите правый Control, "
                         "чтобы выключить.")
        # Диктор здесь молчит: речь перебивает игру. Нажатие слышно
        # коротким сигналом — по нему понятно, что микрофон пошёл в эфир.
        sounds.play("ptt_on")
        self._ptt_watch_start()
        return True

    def _ptt_release(self, event) -> None:
        if not self._ptt_held:
            return
        if event.key() not in (Qt.Key.Key_Control, Qt.Key.Key_unknown):
            return
        # Пока правый Control зажат физически, не выключаем: отпустили
        # другую клавишу или левый Control.
        if _right_ctrl_down():
            return
        self._ptt_off()

    def _ptt_off(self) -> None:
        """Control отпустили или окно потеряло фокус: снова выключаем."""
        self._ptt_watch_stop()
        if not self._ptt_held:
            return
        self._ptt_held = False
        self._mic_muted = True
        self.engine.set_mic_muted(True)
        self._update_mic_button()
        self._set_status("Микрофон выключен.")
        # Отпустили Control — второй короткий сигнал, тоже без диктора.
        sounds.play("ptt_off")

    # -- импорт треков: в обе стороны ---------------------------------------

    def _import_audio(self) -> None:
        """Просит у собеседника список треков из его папки Media."""
        if self.is_teacher:
            if self.server is None or not self.connected:
                self._set_status("Импорт доступен, когда ученик в кабинете.")
                return
            if self.server.request_media_list():
                self._set_status("Запрашиваю у ученика список треков.")
            return
        if self.client is None:
            self._set_status("Импорт доступен при подключении к преподавателю.")
            return
        if self.client.request_media_list():
            self._set_status("Запрашиваю у преподавателя список треков.")

    def _on_media_list(self, tracks) -> None:
        # Сеть присылает уже готовые имена, но старые сборки могли отдавать
        # словари — принимаем оба вида, чтобы импорт не падал с
        # «'str' object has no attribute 'get'».
        who = "у ученика" if self.is_teacher else "у преподавателя"
        names = []
        for t in (tracks or []):
            name = str(t.get("name") or "") if isinstance(t, dict) else str(t or "")
            if name:
                names.append(name)
        if not names:
            text = f"В папке Media {who} нет треков."
            self._set_status(text)
            announce(self.media_combo, text)
            return
        if len(names) == 1:
            choice = names[0]
        else:
            choice, ok = QInputDialog.getItem(
                self, "Импортировать аудио", "Трек:", names, 0, False)
            if not ok or not choice:
                return
        if self.is_teacher:
            asked = (self.server is not None
                     and self.server.request_media(choice))
        else:
            asked = (self.client is not None
                     and self.client.request_media(choice))
        if not asked:
            self._set_status("Не удалось запросить трек: нет соединения.")
            return
        self._set_status(f"Скачиваю трек: {choice}.")

    def _on_media_saved(self, path: str) -> None:
        name = Path(str(path)).name
        appeared = self._refresh_tracks(select=str(path))
        text = f"Трек импортирован в папку Media: {name}."
        if "player" in appeared:
            text += " Плеер готов."
        self._set_status(text)
        self._append_chat("система", text)

    # -- громкость ----------------------------------------------------------

    def _volume_bus(self) -> dict:
        """Набор громкостей активного режима."""
        if self.volume_mode == "peer":
            return self.cfg["audio"].setdefault("volumes_peer", {})
        return self.cfg["audio"].setdefault("volumes", {})

    def _on_volume(self, key: str, title: str, value: int) -> None:
        peer = self.volume_mode == "peer"
        self.engine.set_volume(key, value, peer=peer)
        self._volume_bus()[key] = int(value)
        self.slider_values[key].setText(f"{value} процентов")
        self._save_timer.start()
        self._student_save_timer.start()

    def _apply_volumes(self) -> None:
        # Открыли звук — применяем обе стороны: ползунки показывают только
        # одну, а собеседник должен слышать свою громкость с самого начала.
        for peer in (False, True):
            bus = (self.cfg["audio"].get("volumes_peer") if peer
                   else self.cfg["audio"].get("volumes")) or {}
            for key, value in bus.items():
                self.engine.set_volume(key, int(value), peer=peer)

    def _toggle_volume_mode(self) -> None:
        self.volume_mode = "local" if self.volume_mode == "peer" else "peer"
        self.cfg["audio"]["volume_mode"] = self.volume_mode
        self._save_timer.start()
        self._student_save_timer.start()
        self._refresh_volume_mode()
        self._set_status(f"{self.volume_mode_button.text()}.")

    def _refresh_volume_mode(self) -> None:
        """Подпись переключателя и значения ползунков под активный режим."""
        peer = self.volume_mode == "peer"
        if peer:
            title = ("Громкость ученика" if self.is_teacher
                     else "Громкость преподавателя")
        else:
            title = "Моя громкость"
        self.volume_mode_button.setText(title)
        self.volume_mode_button.setAccessibleName(title)
        bus = self._volume_bus()
        for key, slider in self.sliders.items():
            slider.blockSignals(True)
            slider.setValue(int(bus.get(key, 100)))
            slider.blockSignals(False)
            self.slider_values[key].setText(f"{slider.value()} процентов")
        self._refresh_volume_visibility()

    def _save_student_settings(self) -> None:
        """Громкости текущего урока — в файл этого ученика."""
        if not self.is_teacher or not self._lesson_student:
            return
        audio_cfg = self.cfg["audio"]
        students.save(
            self._lesson_student,
            audio_cfg.get("volumes") or {},
            audio_cfg.get("volumes_peer") or {},
            self.volume_mode,
        )

    def _restore_student_settings(self, student: str) -> bool:
        """Возвращает громкости, с которыми шёл прошлый урок с этим учеником."""
        data = students.load(student)
        if not data:
            return False
        for field in ("volumes", "volumes_peer"):
            saved = data.get(field)
            if not isinstance(saved, dict):
                continue
            bus = self.cfg["audio"].setdefault(field, {})
            for key, value in saved.items():
                if key not in bus:
                    continue
                try:
                    bus[key] = max(0, min(100, int(value)))
                except (TypeError, ValueError):
                    continue
        mode = str(data.get("volume_mode") or "")
        if mode in ("local", "peer"):
            self.volume_mode = mode
            self.cfg["audio"]["volume_mode"] = mode
        if self.engine.running:
            self._apply_volumes()
        self._refresh_volume_mode()
        self._save_timer.start()
        return True

    # -- метроном -----------------------------------------------------------

    def _update_metro_button(self) -> None:
        """Подпись кнопки, доступное имя и видимость полей темпа и ритма."""
        enabled = self.metro_button.isChecked()
        if enabled:
            self.metro_button.setText("Выключить метроном")
        else:
            self.metro_button.setText("Включить метроном")
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
        if checked:
            # Человек сам включил метроном — значит, кнопкой, а не
            # приостановкой ради собеседника.
            self._metro_held = False
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
        self._send_metro_state(checked)

    def _on_metronome_value(self) -> None:
        cfg_metro = self.cfg.setdefault("metronome", {})
        cfg_metro["bpm"] = self.bpm_spin.value()
        cfg_metro["beats"] = self.beats_spin.value()
        self._update_metro_button()
        self.engine.set_metronome(self.metro_button.isChecked(),
                                  self.bpm_spin.value(), self.beats_spin.value())
        self._save_timer.start()
        if self.metro_button.isChecked():
            # Собеседник слышит наш метроном потоком — темп и доли ему
            # нужны для голосового объявления.
            self._send_metro_state(True)

    # -- плеер ---------------------------------------------------------------

    def _skip_seconds(self) -> int:
        try:
            return int((self.cfg.get("player") or {}).get("skip", 5) or 5)
        except (TypeError, ValueError):
            return 5

    def _stretch_algo(self) -> str:
        """Способ растяжения из настроек: «vocoder» или «wsola»."""
        algo = str((self.cfg.get("player") or {}).get(
            "stretch", effects.DEFAULT_ALGO) or effects.DEFAULT_ALGO)
        return algo if algo in effects.ALGOS else effects.DEFAULT_ALGO

    def _selected_track(self) -> str:
        data = self.track_combo.currentData()
        return str(data) if data else ""

    def _refresh_tracks(self, select: str | None = None) -> list[str]:
        """Перечитывает папку Media и прячет кнопки, если треков нет."""
        tracks = media.list_tracks()
        wanted = select or self._selected_track() or str(
            (self.cfg.get("player") or {}).get("last", ""))
        self.track_combo.blockSignals(True)
        self.track_combo.clear()
        for path in tracks:
            self.track_combo.addItem(path.name, str(path))
        index = self.track_combo.findData(wanted)
        if index < 0 and tracks:
            index = 0
        self.track_combo.setCurrentIndex(index)
        self.track_combo.blockSignals(False)
        self._tracks_available = bool(tracks)
        self.player_buttons_row.setVisible(self._tracks_available)
        self.track_position_label.setVisible(self._tracks_available)
        self.effects_row.setVisible(self._tracks_available and self._effects_shown)
        if not self._tracks_available:
            self.track_position_label.setText("")
        self._update_player_buttons()
        if tracks and index >= 0:
            self.cfg.setdefault("player", {})["last"] = self.track_combo.itemData(index)
            self._save_timer.start()
        return self._refresh_volume_visibility()

    def _on_track_changed(self, index: int) -> None:
        player = self.engine.player
        if player.playing:
            player.stop()
            self._set_status("Воспроизведение остановлено: выбран другой трек.")
        elif player.paused:
            # Пауза относилась к прежнему треку: новый должен начаться
            # с начала, а не с места, где остановился старый.
            player.stop()
            self._set_status("Выбран другой трек, пауза снята.")
        self.cfg.setdefault("player", {})["last"] = self._selected_track()
        self._save_timer.start()
        self._update_player_buttons()

    def _add_track(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Добавить трек", str(media.media_dir()),
            "Аудио (*.mp3 *.wav *.flac *.ogg)")
        if not paths:
            return
        added: list[Path] = []
        errors: list[str] = []
        for path in paths:
            try:
                added.append(media.add_track(path))
            except Exception as exc:
                errors.append(f"{Path(path).name}: {exc}")
        if not added:
            self._set_status("Не удалось добавить файл. " + "; ".join(errors))
            return
        appeared = self._refresh_tracks(select=str(added[-1]))
        last = added[-1].name
        if len(added) == 1:
            text = (f"Трек добавлен: {last}. "
                    "Он лежит в папке Data\\Media рядом с программой.")
        else:
            text = f"Добавлено треков: {len(added)}. Выбран последний: {last}."
        if "player" in appeared:
            text += " Появились кнопки плеера и регулятор громкости трека."
        if self.engine.player.playing:
            # Список теперь показывает новый трек — старый дальше не играет.
            self.engine.player.stop()
            self._update_player_buttons()
            text += " Воспроизведение остановлено — выбран новый трек."
        if errors:
            text += " Не получилось добавить: " + "; ".join(errors)
        self._set_status(text)

    def _delete_track(self) -> None:
        path = self._selected_track()
        if not path:
            return
        name = Path(path).name
        box = QMessageBox(self)
        box.setWindowTitle("Удалить трек")
        box.setIcon(QMessageBox.Question)
        box.setText("Вы уверены, что хотите удалить этот трек?")
        box.setInformativeText(f"Трек: {name}. Файл удалится из папки Media.")
        box.setAccessibleName("Удаление трека")
        box.setAccessibleDescription(
            "Вы уверены, что хотите удалить этот трек? Да или нет.")
        delete = box.addButton("Да", QMessageBox.DestructiveRole)
        cancel = box.addButton("Нет", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not delete:
            self._set_status("Удаление отменено.")
            return
        player = self.engine.player
        if player.path == path:
            player.clear()
            self._track_source = None
            self._effects_applied_speed = 1.0
            self._clear_effects_cache()
            self._effects_gen += 1
            self._effects_key = (1.0, 0)
            self._effects_path = ""
        try:
            Path(path).unlink()
        except OSError as exc:
            self._set_status(f"Не удалось удалить трек: {exc}")
            return
        self._refresh_tracks()
        self._update_player_buttons()
        self._set_status(f"Трек удалён: {name}")

    def _toggle_play(self) -> None:
        """Одна кнопка: играть, остановить или продолжить с паузы."""
        player = self.engine.player
        if player.playing:
            player.stop()
            self._update_player_buttons()
            self._set_status("Трек остановлен. Следующее включение — с начала.")
            return
        if player.paused:
            name = player.name or "трек"
            if player.play():
                self._update_player_buttons()
                self._set_status(f"Продолжаю: {name}")
            return
        self._play_selected()

    def _play_selected(self) -> None:
        """Запускает трек, выбранный в списке."""
        player = self.engine.player
        path = self._selected_track()
        if not path:
            self._set_status("Сначала добавьте трек кнопкой Добавить трек.")
            return
        if not self.engine.running:
            self._set_status("Звук не запущен. Откройте кабинет или "
                             "подключитесь, потом включайте трек.")
            return
        if player.path == path and player.has_track:
            if player.play():
                self._update_player_buttons()
                self._set_status(f"Играет: {Path(path).name}")
            return
        self._load_and_play(path)

    def _pause_play(self) -> None:
        player = self.engine.player
        if not player.pause():
            self._set_status("Сейчас трек не играет, пауза не нужна.")
            return
        self._update_player_buttons()
        self._set_status(f"Пауза. Позиция {_clock(player.position())}.")

    def _step_track(self, direction: int) -> None:
        """«Выше» и «Ниже»: переключение на предыдущий и следующий трек.

        Кнопка меняет не только выделение в списке, но и сам трек в плеере.
        Если музыка играла, включается следующий; если стояла на паузе или
        звук ещё не запущен, трек просто переключается и ждёт кнопки
        «Играть» — молча включить музыку было бы сюрпризом.
        """
        count = self.track_combo.count()
        if count == 0:
            self._set_status("Список треков пуст. Добавьте трек.")
            return
        if count == 1:
            self._set_status("В списке всего один трек.")
            return
        index = self.track_combo.currentIndex()
        new = (index + direction) % count if index >= 0 else 0
        # Переключение делаем молча: объявим один раз, уже с новым треком.
        self.track_combo.blockSignals(True)
        self.track_combo.setCurrentIndex(new)
        self.track_combo.blockSignals(False)
        path = self._selected_track()
        self.cfg.setdefault("player", {})["last"] = path
        self._save_timer.start()
        player = self.engine.player
        # Играло — играет и новый трек. Стояло на паузе или звук не открыт —
        # новый трек занимает место прежнего, но сам не включается.
        play = self.engine.running and player.playing
        self._load_and_play(path, play=play)

    def _delete_key_targets_track(self) -> bool:
        """Нажата ли клавиша Delete там, где выбран трек списка."""
        focus = QApplication.focusWidget()
        if focus is None:
            return False
        if focus is self.track_combo:
            return True
        try:
            view = self.track_combo.view()
            return (focus is view or focus is view.window()
                    or view.isAncestorOf(focus))
        except Exception:
            return False

    def _load_and_play(self, path: str, play: bool = True) -> None:
        """Загружает трек в плеер. play — включать ли звук сразу."""
        if self._loading_track:
            # Нажали ещё раз, пока грузится прошлый: запоминаем последний
            # выбор — он и загрузится, когда закончится текущая загрузка.
            self._pending_track = path
            self._pending_play = play
            return
        self._loading_track = True
        self._load_play = play
        self._set_status(f"Загружаю трек {Path(path).name}…")
        threading.Thread(target=self._load_track_worker, args=(path,),
                         daemon=True).start()

    def _load_track_worker(self, path: str) -> None:
        try:
            data = media.load_track(path)
        except Exception as exc:
            self.sig_track_error.emit(f"Не удалось открыть трек: {exc}")
            return
        self.sig_track_ready.emit(path, data)

    def _on_track_ready(self, path: str, data) -> None:
        self._loading_track = False
        # Пока грузился этот трек, успели выбрать другой, а то и вернуться
        # к этому же: приоритет у последнего нажатия.
        pending, self._pending_track = self._pending_track, ""
        if pending and pending != path:
            self._load_and_play(pending, self._pending_play)
            return
        if pending:
            self._load_play = self._pending_play
        # Исходный звук храним отдельно: скорость и тон всегда считаются
        # от него, а не от уже обработанного варианта.
        self._track_source = data
        # Другой трек — другой звук: готовые варианты прежнего бесполезны,
        # а недосчитанный результат прошлого трека отбрасываем номером.
        self._clear_effects_cache()
        self._effects_gen += 1
        self._effects_key = (1.0, 0)
        self._effects_path = path
        if self._effects_active():
            self._set_status("Применяю эффект…")
            self._start_effects_worker()
            return
        self._start_playing_loaded(path, data, play=self._load_play)

    def _start_playing_loaded(self, path: str, data, prefix: str = "",
                              applied_speed: float = 1.0,
                              play: bool = True) -> None:
        player = self.engine.player
        player.set_track(path, data)
        self._effects_applied_speed = applied_speed
        self._load_play = True
        duration = _clock(player.duration())
        self.track_position_label.setText(f"0:00 из {duration}")
        if not play:
            # Трек переключён, но сам не зазвучал: следующий шаг — «Играть».
            self._update_player_buttons()
            text = (f"Выбран трек: {player.name}. Не играет. "
                    f"Позиция 0:00 из {duration}. Кнопка Играть включит его.")
            self._set_status(f"{prefix} {text}" if prefix else text)
            return
        player.play()
        self._update_player_buttons()
        text = f"Играет: {player.name}. Позиция 0:00 из {duration}."
        self._set_status(f"{prefix} {text}" if prefix else text)

    def _on_track_error(self, text: str) -> None:
        self._loading_track = False
        pending, self._pending_track = self._pending_track, ""
        if pending:
            self._load_and_play(pending, self._pending_play)
            return
        self._set_status(text)

    # -- эффекты плеера ------------------------------------------------------

    def _toggle_effects(self) -> None:
        """Показывает и прячет кнопки скорости и тона."""
        self._effects_shown = not self._effects_shown
        self.effects_row.setVisible(self._effects_shown and self._tracks_available)
        if self._effects_shown:
            text = f"Эффекты показаны. {self._effects_word().capitalize()}."
            hint = self._resets_word()
            self._set_status(f"{text} {hint}" if hint else text)
        else:
            self._set_status("Эффекты скрыты.")

    def _effects_word(self) -> str:
        parts = []
        if abs(self._speed - 1.0) > 1e-6:
            parts.append(f"скорость {_percent(self._speed)}")
        if self._pitch:
            parts.append(_pitch_word(self._pitch))
        return ", ".join(parts) if parts else "обычные скорость и тон"

    def _resets_word(self) -> str:
        """Подсказка про кнопки сброса для диктора."""
        speed = abs(self._speed - 1.0) > 1e-6
        pitch = self._pitch != 0
        if speed and pitch:
            return "Доступны кнопки Сброс скорости и Сброс тона."
        if speed:
            return "Доступна кнопка Сброс скорости."
        if pitch:
            return "Доступна кнопка Сброс тона."
        return ""

    def _effects_active(self) -> bool:
        return abs(self._speed - 1.0) > 1e-6 or self._pitch != 0

    def _change_speed(self, direction: int) -> None:
        old = self._speed
        new = round(min(_SPEED_MAX, max(_SPEED_MIN,
                                        old + direction * _SPEED_STEP)), 2)
        if abs(new - old) < 1e-6:
            self._set_status(
                ("Быстрее" if direction > 0 else "Медленнее")
                + f" уже нельзя: скорость {_percent(old)}.")
            return
        self._speed = new
        self._apply_effects()

    def _change_pitch(self, direction: int) -> None:
        old = self._pitch
        new = max(-_PITCH_LIMIT, min(_PITCH_LIMIT, old + direction))
        if new == old:
            self._set_status(
                ("Выше" if direction > 0 else "Ниже")
                + f" уже нельзя: {_pitch_word(old)}.")
            return
        self._pitch = new
        self._apply_effects()

    def _reset_speed(self) -> None:
        # Проверяем не только настройку, но и то, какой вариант сейчас
        # звучит: настройка и звук могут разойтись, и тогда сброс должен
        # всё равно вернуть исходный трек.
        if abs(self._speed - 1.0) < 1e-6 and self._effects_key[0] == 1.0:
            self._set_status("Скорость уже обычная.")
            return
        self._speed = 1.0
        self._apply_effects()

    def _reset_pitch(self) -> None:
        if not self._pitch and self._effects_key[1] == 0:
            self._set_status("Тон уже обычный.")
            return
        self._pitch = 0
        self._apply_effects()

    def _update_effects_buttons(self) -> str:
        """Кнопки сброса появляются, только когда есть что сбрасывать.

        Возвращает фразу для диктора: появление и исчезновение кнопки
        само по себе не проговаривается, а знать о ней нужно.
        """
        speed = abs(self._speed - 1.0) > 1e-6
        pitch = self._pitch != 0
        notes = []
        if speed != (not self.speed_reset_button.isHidden()):
            self.speed_reset_button.setVisible(speed)
            notes.append("Появилась кнопка Сброс скорости." if speed
                         else "Кнопка Сброс скорости убрана.")
        if pitch != (not self.pitch_reset_button.isHidden()):
            self.pitch_reset_button.setVisible(pitch)
            notes.append("Появилась кнопка Сброс тона." if pitch
                         else "Кнопка Сброс тона убрана.")
        return " ".join(notes)

    def _apply_effects(self) -> None:
        """Пересчитывает трек под текущие скорость и тон."""
        hint = self._update_effects_buttons()
        self._effects_hint = hint
        # Новый номер настройки: результат прошлого пересчёта уже не нужен.
        self._effects_gen += 1
        player = self.engine.player
        path = self._selected_track()
        if self._track_source is None or not (
                player.has_track and player.path == path):
            if self._effects_active():
                text = (f"{self._effects_word().capitalize()}. "
                        "Эффект применится, когда включите трек.")
            else:
                text = "Эффекты вернулись к обычным."
            # Подсказку про кнопки сброса уже сказали — второй раз не надо.
            self._effects_hint = ""
            self._set_status(f"{text} {hint}" if hint else text)
            return
        self._effects_path = path
        if not self._effects_active():
            # Обычные скорость и тон — это сам исходный звук. Он уже лежит
            # в памяти, поэтому возвращаемся к нему сразу, не дожидаясь,
            # пока досчитается вариант с прошлой настройкой.
            self._place_effects(self._track_source, 1.0)
            return
        key = (self._speed, self._pitch)
        cached = self._effects_cache.get(key)
        if cached is not None:
            # Такую настройку уже считали — ставим готовый вариант сразу.
            self._place_effects(cached, key[0])
            return
        if self._effects_busy:
            # Считается прошлая настройка. Когда закончит, увидит, что
            # нужна уже другая, и пересчитает её сам.
            text = (f"{self._effects_word().capitalize()}. "
                    "Применю, когда закончится текущий пересчёт.")
            self._effects_hint = ""
            self._set_status(f"{text} {hint}" if hint else text)
            return
        self._set_status("Применяю эффект…")
        self._start_effects_worker()

    def _start_effects_worker(self) -> None:
        source = self._track_source
        if source is None:
            return
        self._effects_busy = True
        self._effects_job_gen = self._effects_gen
        threading.Thread(
            target=self._effects_worker,
            args=(source, self.engine.player.rate, self._speed, self._pitch,
                  self._effects_gen, self._stretch_algo()),
            daemon=True).start()

    def _effects_worker(self, source, rate, speed, pitch, gen,
                        algo: str) -> None:
        try:
            data = effects.apply(source, rate, speed, pitch, algo=algo)
        except Exception as exc:
            self.sig_effects_error.emit(
                f"Не удалось применить эффект: {_explain(exc)}", gen)
            return
        self.sig_effects_ready.emit(data, gen, speed, pitch)

    def _place_effects(self, data, speed_used: float) -> None:
        """Ставит готовый вариант трека в плеер, сохраняя место."""
        path = self._effects_path
        hint = self._effects_hint
        self._effects_hint = ""
        if data is None or not path or path != self._selected_track():
            return
        player = self.engine.player
        self._effects_key = ((1.0, 0) if not self._effects_active()
                             else (self._speed, self._pitch))
        if not (player.has_track and player.path == path):
            # Трек ещё не был загружен: включаем сразу с нужным звуком.
            prefix = f"{self._effects_word().capitalize()}."
            if hint:
                prefix = f"{prefix} {hint}"
            self._start_playing_loaded(path, data, prefix=prefix,
                                       applied_speed=speed_used,
                                       play=self._load_play)
            return
        was_playing = player.playing
        # Место в исходном треке: текущая позиция умножается на скорость,
        # с которой звучит загруженный вариант.
        source_pos = player.position() * self._effects_applied_speed
        player.replace(data, position=source_pos / speed_used)
        if was_playing and player.position() < player.duration():
            player.play()
        self._effects_applied_speed = speed_used
        self._update_player_buttons()
        duration = _clock(player.duration())
        position = player.position()
        self.track_position_label.setText(f"{_clock(position)} из {duration}")
        text = (f"{self._effects_word().capitalize()}. "
                f"Позиция {_clock(position)} из {duration}.")
        self._set_status(f"{text} {hint}" if hint else text)

    def _remember_effects(self, key: tuple[float, int], data) -> None:
        """Держит последние варианты трека, чтобы возврат был мгновенным."""
        size = int(getattr(data, "nbytes", 0))
        if size <= 0 or size > _EFFECTS_CACHE_BYTES:
            return
        old = self._effects_cache.pop(key, None)
        if old is not None:
            self._effects_cache_bytes -= int(old.nbytes)
        self._effects_cache[key] = data
        self._effects_cache_bytes += size
        # Память не бесконечна: вытесняем самые старые варианты.
        while (self._effects_cache_bytes > _EFFECTS_CACHE_BYTES
               and len(self._effects_cache) > 1):
            oldest = next(iter(self._effects_cache))
            if oldest == key:
                break
            dropped = self._effects_cache.pop(oldest)
            self._effects_cache_bytes -= int(dropped.nbytes)

    def _clear_effects_cache(self) -> None:
        self._effects_cache.clear()
        self._effects_cache_bytes = 0

    def _continue_effects(self) -> None:
        """Доводит до конца настройку, которую сменили во время пересчёта."""
        if not self._effects_active():
            if self._effects_key != (1.0, 0):
                self._place_effects(self._track_source, 1.0)
            return
        key = (self._speed, self._pitch)
        if self._effects_key == key:
            # Нужный вариант уже стоит в плеере.
            return
        cached = self._effects_cache.get(key)
        if cached is not None:
            self._place_effects(cached, key[0])
            return
        self._start_effects_worker()

    def _on_effects_ready(self, data, gen: int, speed: float,
                          pitch: float) -> None:
        if gen == self._effects_job_gen:
            self._effects_busy = False
        key = (speed, pitch)
        if gen != self._effects_gen and key != (self._speed, self._pitch):
            # Пока считали, настройку сменили — этот вариант устарел.
            self._continue_effects()
            return
        path = self._effects_path
        if not path or path != self._selected_track():
            # Трек успели сменить или удалить — результат уже не нужен.
            return
        self._remember_effects(key, data)
        self._place_effects(data, speed)

    def _on_effects_error(self, text: str, gen: int = 0) -> None:
        if gen == self._effects_job_gen:
            self._effects_busy = False
        hint = self._effects_hint
        self._effects_hint = ""
        self._set_status(f"{text} {hint}" if hint else text)

    def _seek(self, direction: int) -> None:
        player = self.engine.player
        path = self._selected_track()
        if not path:
            return
        if not (player.path == path and player.has_track):
            self._set_status("Сначала нажмите Играть: трек загрузится, "
                             "и перемотка станет доступна.")
            return
        step = self._skip_seconds()
        position = player.seek(direction * step)
        # Перемотка за конец — это не «доиграл», отдельного объявления не надо.
        player.take_finished()
        duration = _clock(player.duration())
        self.track_position_label.setText(f"{_clock(position)} из {duration}")
        self._update_player_buttons()
        word = "Назад" if direction < 0 else "Вперёд"
        self._set_status(f"{word} на {step} секунд. "
                         f"Позиция {_clock(position)} из {duration}.")

    def _tick_midi(self) -> None:
        """Автоподхват MIDI-клавиатуры: воткнули или выдернули — без настроек."""
        if not self.engine.running:
            return
        try:
            self.engine.ensure_midi()
        except Exception as exc:
            self._log_threadsafe(f"Ошибка автоподхвата MIDI: {exc}")

    def _tick_player(self) -> None:
        player = self.engine.player
        if player.take_finished() and not player.playing:
            self._update_player_buttons()
            self._set_status(f"Трек закончился: {player.name or 'трек'}.")
        if not player.has_track:
            return
        text = f"{_clock(player.position())} из {_clock(player.duration())}"
        if self.track_position_label.text() != text:
            self.track_position_label.setText(text)
        self._update_player_buttons()

    def _update_player_buttons(self) -> None:
        """Подписи кнопок плеера под текущее состояние."""
        player = self.engine.player
        playing = player.playing
        paused = player.paused
        skip = self._skip_seconds()
        state = (playing, paused, skip)
        if state == self._player_button_state:
            return
        self._player_button_state = state
        # Одна кнопка на три состояния: остановлен — «Играть», играет —
        # «Стоп», на паузе — «Продолжить» (трек стоит, место сохранено).
        if playing:
            text, name = "Стоп", "Остановить трек"
        elif paused:
            text, name = "Продолжить", "Продолжить трек"
        else:
            text, name = "Играть", "Играть трек"
        self.play_button.setText(text)
        self.play_button.setAccessibleName(name)
        self.pause_button.setEnabled(playing)
        # Шаг перемотки остаётся в настройках, на кнопке он не нужен.
        self.back_button.setText("Назад")
        self.back_button.setAccessibleName("Назад")
        self.forward_button.setText("Вперёд")
        self.forward_button.setAccessibleName("Вперёд")

    def _refresh_volume_visibility(self) -> list[str]:
        """Оставляет на экране только регуляторы выбранных устройств.

        Громкость собеседника есть всегда. Остальные регуляторы появляются,
        когда устройство выбрано в настройках, и прячутся, когда его убрали.
        Возвращает ключи регуляторов, которые только что появились.
        """
        audio_cfg = self.cfg.get("audio", {})
        peer = self.volume_mode == "peer"
        active = {
            # Свою сторону собеседник не регулирует, а его сторону —
            # свои входы: поэтому наборы разные. Первый вход виден в обоих
            # режимах: в «Моей громкости» он задаёт громкость
            # самопрослушивания (насколько громко себя слышно в наушниках),
            # в «Громкости ученика» — насколько громко его слышит ученик.
            "student": not peer,
            "in1": bool(str(audio_cfg.get("device1") or "").strip()),
            "in2": peer and bool(str(audio_cfg.get("device2") or "").strip()),
            "usb": peer and bool(str(audio_cfg.get("usb") or "").strip()),
            "midi": bool(audio_cfg.get("midi_enabled")),
            "metronome": bool(self.cfg.get("metronome", {}).get("enabled")),
            "player": bool(self._tracks_available),
        }
        # Первый вход в «Моей громкости» звучит только при включённом
        # самопрослушивании: подсказываем это диктору, чтобы регулятор не
        # выглядел немым.
        slider = self.sliders.get("in1")
        if slider is not None:
            if peer:
                slider.setAccessibleDescription(
                    "Насколько громко первый вход слышит собеседник.")
            else:
                slider.setAccessibleDescription(
                    "Громкость самопрослушивания: насколько громко вы "
                    "слышите себя в наушниках, если включено «Слышать "
                    "себя».")
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
        mid = None
        if self.is_teacher and self.server is not None:
            mid = self.server.send_chat(text)
        elif self.client is not None:
            mid = self.client.send_chat(text)
        if mid:
            self._append_chat("вы", text, mid)
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
            self._append_chat("система", f"Файл отправлен: {Path(path).name}",
                              file_path=path)
        else:
            self._set_status("Файл не отправлен: нет соединения.")

    def _on_file_message(self, who: str, path: str) -> None:
        """Пришёл файл: строка в чате с привязкой к файлу на диске.

        Сам файл уже сохранён в папке Data/received. Путь к нему записан
        в строку: по нему кнопка «Скачать» и меню кладут файл в Media.
        """
        name = os.path.basename(str(path or ""))
        self._append_chat("система", f"{who} прислал файл: {name}",
                          file_path=str(path or ""))

    def _open_chat_link(self) -> None:
        """Открывает ссылку из выбранного сообщения в браузере."""
        item = self.chat_list.currentItem()
        if item is None:
            announce(self.chat_list, "Нет выбранного сообщения.")
            return
        link = find_link(str(item.data(Qt.UserRole + 3) or ""))
        if not link:
            announce(self.chat_list, "В этом сообщении нет ссылки.")
            return
        if not QDesktopServices.openUrl(QUrl(link)):
            text = f"Не удалось открыть ссылку: {link}"
            self._set_status(text)
            announce(self.chat_list, text)
            return
        announce(self.chat_list, f"Открываю ссылку: {link}")

    def _download_chat_file(self, item=None) -> None:
        """Кладёт файл из выбранного сообщения в папку Media.

        Присланный файл уже лежит в Data/received — «скачать» значит
        скопировать его в Media, откуда он сразу виден в плеере.
        """
        if not isinstance(item, QListWidgetItem):
            item = self.chat_list.currentItem()
        if item is None:
            announce(self.chat_list, "Выберите сообщение с файлом.")
            return
        path = str(item.data(Qt.UserRole + 4) or "")
        if not path:
            announce(self.chat_list, "У этого сообщения нет файла. "
                                     "Выберите сообщение, где написано, "
                                     "что прислан файл.")
            return
        name = os.path.basename(path)
        if not os.path.exists(path):
            text = f"Файл {name} не найден: его могли удалить."
            self._set_status(text)
            announce(self.chat_list, text)
            return
        announce(self.chat_list, f"Скачивание файла {name} началось.")
        self._set_status(f"Скачиваю файл: {name}.")
        try:
            target = media.add_track(path)
        except OSError as exc:
            text = f"Не удалось скачать файл {name}: {exc}"
            self._set_status(text)
            announce(self.chat_list, text)
            return
        self._refresh_tracks(select=str(target))
        text = f"Файл скачан в папку Media: {target.name}."
        self._set_status(text)
        announce(self.chat_list, text)

    def _add_chat_item(self, sender: str, text: str, mid: str = "",
                       when: datetime | None = None,
                       row: int | None = None,
                       file_path: str = "") -> QListWidgetItem:
        """Создаёт строку чата. row задан — вставляет на это место."""
        item = QListWidgetItem()
        item.setData(Qt.UserRole, mid or f"local-{uuid.uuid4().hex}")
        item.setData(Qt.UserRole + 1, when or datetime.now())
        item.setData(Qt.UserRole + 2, sender)
        item.setData(Qt.UserRole + 3, text)
        item.setData(Qt.UserRole + 4, file_path)
        if row is None:
            self.chat_list.addItem(item)
        else:
            self.chat_list.insertItem(row, item)
        return item

    def _append_chat(self, sender: str, text: str, mid: str = "",
                     keep: bool = False, owner: str = "",
                     file_path: str = "") -> None:
        """Добавляет строку в чат.

        У каждой строки есть номер отправителя и время. Время пишется после
        сообщения; у первого сообщения нового дня перед временем стоит дата,
        чтобы было понятно, в какой день шёл разговор. keep — строку надо
        сохранить в историю, даже если это заметка программы (конец урока).
        owner — в файл какого ученика её писать, если кабинет уже перешёл
        к следующему. file_path — файл, который пришёл или ушёл этой строкой.
        """
        self._add_chat_item(sender, text, mid, file_path=file_path)
        self._record_history(sender, text, mid, keep=keep, owner=owner)
        self._refresh_chat()
        self.chat_list.scrollToBottom()
        if sender in ("система", "вы"):
            return
        # Настоящее входящее сообщение: звук и озвучка диктором. Свои
        # реплики и строки состояния читать вслух не нужно.
        sounds.play("message")
        announce(self.chat_list, f"Новое сообщение от {sender}: {text}")

    def _record_history(self, sender: str, text: str, mid: str,
                        keep: bool = False, owner: str = "") -> None:
        """Запоминает сообщение переписки и сохраняет файл ученика.

        В историю идут настоящие сообщения. Строки состояния и подсказки
        программы разговором не считаются — исключение keep: заметки
        вроде окончания урока владелец потом просматривает.
        """
        if not self.is_teacher or not mid or mid.startswith("local-"):
            return
        if sender == "система" and not keep:
            return
        # «вы» — подпись на экране преподавателя. В файле это должно быть
        # его имя: ученик прочтёт «вы» как себя самого.
        if sender == "вы":
            sender = str(self.cfg.get("name") or "Преподаватель")
        self._history.append({
            "id": mid,
            "time": datetime.now().isoformat(timespec="seconds"),
            "sender": sender,
            "text": text,
        })
        if len(self._history) > history.MAX_MESSAGES:
            # Режем на месте: у сервера тот же список, его нельзя заменять.
            del self._history[:len(self._history) - history.MAX_MESSAGES]
        student = owner or self._history_student_name()
        if student:
            history.save(self._history, student)

    def _history_student_name(self) -> str:
        """Ученик, к чьей переписке относится то, что сейчас на экране.

        Пока ученик в кабинете — он; когда отключился — тот, чья переписка
        осталась открыта, чтобы правка и удаление после урока тоже попали
        в его файл.
        """
        active = self.server.active_name if self.server is not None else ""
        return str(active or self._history_student)

    def _on_history_loaded(self, student: str) -> None:
        """Показывает переписку ученика, который вошёл в кабинет.

        У каждого ученика свой разговор, поэтому при смене ученика чат
        пересобирается заново: сначала последняя порция, а более ранняя
        подтягивается, когда преподаватель листает чат вверх.
        """
        self._history_student = str(student or "")
        chunk, more = history.page(self._history, "", PAGE)
        self._history_more = more
        self._history_pending = False
        self._loading_history = True
        try:
            self.chat_list.clear()
            for m in chunk:
                self._add_chat_item(
                    self._display_sender(str(m.get("sender") or "")),
                    str(m.get("text") or ""),
                    str(m.get("id") or ""),
                    self._parse_time(m.get("time")))
        finally:
            self._loading_history = False
        self._refresh_chat()
        self.chat_list.scrollToBottom()
        if chunk:
            announce(self.chat_list,
                     f"Переписка с {student}. Показаны последние сообщения, "
                     f"{len(chunk)}.")
        else:
            announce(self.chat_list, f"Переписки с {student} пока нет.")

    @staticmethod
    def _parse_time(value) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    def _display_sender(self, sender: str) -> str:
        """Свои сообщения в истории чата подписываем «вы»."""
        me = str(self.cfg.get("name") or "").strip()
        if me and sender == me:
            return "вы"
        return sender

    def _oldest_chat_id(self) -> str:
        """Номер самого верхнего настоящего сообщения — от него листаем дальше."""
        for row in range(self.chat_list.count()):
            mid = str(self.chat_list.item(row).data(Qt.UserRole) or "")
            if mid and not mid.startswith("local-"):
                return mid
        return ""

    def _on_chat_row_changed(self, row: int) -> None:
        if row == 0 and not self._loading_history:
            self._load_older()

    def _load_older(self) -> None:
        """Догружает предыдущую порцию переписки над верхним сообщением."""
        if self._history_pending or not self._history_more:
            return
        if self.is_teacher:
            chunk, more = history.page(
                self._history, self._oldest_chat_id(), PAGE)
            if not chunk:
                self._history_more = False
                return
            self._history_more = more
            self._prepend_history(chunk)
        else:
            self._request_history(self._oldest_chat_id())

    def _request_history(self, before: str) -> None:
        """Просит у преподавателя очередную порцию переписки."""
        if self.is_teacher or self.client is None or self._history_pending:
            return
        self._history_pending = True
        if not self.client.request_history(before, PAGE):
            self._history_pending = False

    def _prepend_history(self, chunk: list[dict]) -> None:
        """Ставит порцию истории над уже загруженными сообщениями.

        Верхнее сообщение, на котором стоял курсор, остаётся на месте:
        иначе при подгрузке скринридер перепрыгивал бы на другое место.
        """
        current = self.chat_list.currentItem()
        have = {str(self.chat_list.item(i).data(Qt.UserRole) or "")
                for i in range(self.chat_list.count())}
        chunk = [m for m in chunk
                 if str(m.get("id") or "") not in have]
        if not chunk:
            return
        self._loading_history = True
        try:
            for row, m in enumerate(chunk):
                self._add_chat_item(
                    self._display_sender(str(m.get("sender") or "")),
                    str(m.get("text") or ""),
                    str(m.get("id") or ""),
                    self._parse_time(m.get("time")),
                    row=row)
            self._refresh_chat()
            if current is not None:
                self.chat_list.setCurrentItem(current)
                self.chat_list.scrollToItem(current)
        finally:
            self._loading_history = False
        announce(self.chat_list,
                 f"Загружено прошлых сообщений: {len(chunk)}")

    def _on_history(self, messages, more: bool) -> None:
        self._history_pending = False
        chunk = []
        for m in messages:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            chunk.append({
                "id": str(m.get("id")),
                "time": str(m.get("time") or ""),
                "sender": str(m.get("from") or m.get("sender") or ""),
                "text": str(m.get("text") or ""),
            })
        if not chunk:
            self._history_more = False
            return
        self._prepend_history(chunk)
        self._history_more = bool(more)

    def _history_delete(self, mid: str) -> None:
        """Убирает удалённое сообщение из истории и файла."""
        if not self.is_teacher or not mid:
            return
        before = len(self._history)
        self._history[:] = [m for m in self._history
                            if str(m.get("id") or "") != mid]
        student = self._history_student_name()
        if len(self._history) != before and student:
            history.save(self._history, student)

    def _history_edit(self, mid: str, text: str) -> None:
        """Обновляет изменённое сообщение в истории и файле."""
        if not self.is_teacher or not mid:
            return
        student = self._history_student_name()
        if not student:
            return
        for m in self._history:
            if str(m.get("id") or "") == mid:
                m["text"] = text
                history.save(self._history, student)
                return

    def _refresh_chat(self) -> None:
        """Пересобирает тексты строк: метка дня ставится один раз в сутки."""
        previous_day = None
        for row in range(self.chat_list.count()):
            item = self.chat_list.item(row)
            when = item.data(Qt.UserRole + 1)
            sender = str(item.data(Qt.UserRole + 2) or "")
            text = str(item.data(Qt.UserRole + 3) or "")
            stamp = f"{when:%H:%M}" if isinstance(when, datetime) else ""
            day = (when.year, when.month, when.day) if isinstance(when, datetime) else None
            if day is not None and day != previous_day:
                stamp = f"{when.day} {MONTHS[when.month - 1]}, {stamp}"
            previous_day = day
            line = f"{sender}: {text}"
            if stamp:
                line = f"{line} — {stamp}"
            if item.text() != line:
                item.setText(line)
            # Сообщение со ссылкой синее: так видно, что Enter или меню
            # откроют адрес в браузере. Текст диктор читает как обычно.
            item.setForeground(QBrush(link_color()) if find_link(text)
                               else QBrush())

    def _chat_item(self, mid: str) -> QListWidgetItem | None:
        for row in range(self.chat_list.count()):
            item = self.chat_list.item(row)
            if item.data(Qt.UserRole) == mid:
                return item
        return None

    def _send_chat_delete(self, mid: str) -> None:
        if not mid or mid.startswith("local-"):
            return
        if self.is_teacher and self.server is not None:
            self.server.send_chat_delete(mid)
        elif self.client is not None:
            self.client.send_chat_delete(mid)

    def _send_chat_edit(self, mid: str, text: str) -> None:
        if not mid or mid.startswith("local-"):
            return
        if self.is_teacher and self.server is not None:
            self.server.send_chat_edit(mid, text)
        elif self.client is not None:
            self.client.send_chat_edit(mid, text)

    def _delete_chat_message(self) -> None:
        item = self.chat_list.currentItem()
        if item is None:
            return
        text = str(item.data(Qt.UserRole + 3) or "")
        box = QMessageBox(self)
        box.setWindowTitle("Удалить сообщение")
        box.setIcon(QMessageBox.Question)
        box.setText(f"Удалить сообщение «{text}» у себя и у собеседника?")
        box.setAccessibleName("Удаление сообщения")
        delete = box.addButton("Удалить", QMessageBox.DestructiveRole)
        cancel = box.addButton("Отмена", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not delete:
            self._set_status("Удаление отменено.")
            return
        mid = str(item.data(Qt.UserRole) or "")
        self._send_chat_delete(mid)
        self._history_delete(mid)
        row = self.chat_list.row(item)
        self.chat_list.takeItem(row)
        self._refresh_chat()
        self._set_status("Сообщение удалено.")

    def _edit_chat_message(self) -> None:
        item = self.chat_list.currentItem()
        if item is None:
            return
        old = str(item.data(Qt.UserRole + 3) or "")
        text, ok = QInputDialog.getText(
            self, "Изменить сообщение", "Новый текст:", QLineEdit.Normal, old)
        if not ok:
            return
        text = text.strip()
        if not text:
            return
        item.setData(Qt.UserRole + 3, text)
        self._refresh_chat()
        mid = str(item.data(Qt.UserRole) or "")
        self._send_chat_edit(mid, text)
        self._history_edit(mid, text)
        announce(self.chat_list, f"Сообщение изменено: {text}")

    def _on_chat_delete(self, mid: str) -> None:
        self._history_delete(mid)
        item = self._chat_item(mid)
        if item is None:
            return
        self.chat_list.takeItem(self.chat_list.row(item))
        self._refresh_chat()
        announce(self.chat_list, "Собеседник удалил сообщение.")

    def _on_chat_edit(self, mid: str, text: str) -> None:
        self._history_edit(mid, text)
        item = self._chat_item(mid)
        if item is None:
            return
        item.setData(Qt.UserRole + 3, text)
        self._refresh_chat()
        announce(self.chat_list, f"Собеседник изменил сообщение: {text}")

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        # То же, что видно в строке состояния, ложится в majorlad.log:
        # по этому файлу видно, что случилось до ошибки. Диктору статусные
        # подсказки не читаем: они мешают и всё равно есть в логе.
        runlog.write(text)

    def _log_threadsafe(self, text: str) -> None:
        self.sig_status.emit(text)

    # -- видео --------------------------------------------------------------

    def _on_video_frame(self, image) -> None:
        if not self.cfg["video"].get("show_remote", True):
            return
        self.video_label.setVisible(True)
        self.video_button.setVisible(True)
        pixmap = QPixmap.fromImage(image)
        self.video_label.setPixmap(
            pixmap.scaled(self.video_label.size(), Qt.KeepAspectRatio,
                          Qt.SmoothTransformation)
        )
        if self.video_window is not None:
            self.video_window.show_frame(pixmap)

    # -- сочетания клавиш ---------------------------------------------------

    def _hint(self, action_id: str) -> str:
        """Сочетание в скобках — для подписи кнопки."""
        combo = shortcuts.resolved(self.cfg.get("shortcuts") or {}, action_id)
        return f" ({combo})" if combo else ""

    def _apply_shortcuts(self) -> None:
        """Собирает карту «сочетание — действие» из настроек."""
        saved = self.cfg.get("shortcuts") or {}
        mapping: dict[str, str] = {}
        for action_id, _title in shortcuts.all_actions(self.is_teacher):
            combo = shortcuts.resolved(saved, action_id)
            if not combo:
                continue
            # Одно сочетание — одно действие: при повторе срабатывает
            # первое по списку.
            mapping.setdefault(combo, action_id)
        self._shortcut_map = mapping
        # Действующие сочетания — в журнал: если назначенное сочетание не
        # срабатывает, по этой строке сразу видно, что именно записано.
        runlog.write("Сочетания клавиш: " + (
            ", ".join(f"{action}={combo}"
                      for combo, action in sorted(mapping.items()))
            or "ни одного"))
        self.settings_button.setText(f"Настройки{self._hint('settings')}")
        self.settings_button.setAccessibleName(self.settings_button.text())
        self._update_mic_button()
        self._update_cam_button()

    def _focus_message_edit(self) -> None:
        """Ставит курсор в поле сообщения, не трогая уже набранный текст."""
        self.message_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        announce(self.message_edit, "Поле сообщения")

    def _run_shortcut(self, action_id: str) -> None:
        """Выполняет действие, назначенное на сочетание клавиш."""
        runlog.write(f"Сочетание клавиш: {action_id}")
        if action_id.startswith("volume_"):
            kind, _sep, key = action_id.partition(":")
            slider = self.sliders.get(key)
            if slider is None:
                return
            step = shortcuts.SLIDER_STEP if kind == "volume_up" else -shortcuts.SLIDER_STEP
            value = max(slider.minimum(),
                        min(slider.maximum(), slider.value() + step))
            slider.setValue(value)
            announce(slider,
                     f"{self.slider_titles.get(key, key)}, {value} процентов")
            return
        actions = {
            "volume_mode": self._toggle_volume_mode,
            "metronome": self.metro_button.toggle,
            "add_track": self._add_track,
            "import_audio": self._import_audio,
            "play": self._toggle_play,
            "pause": self._pause_play,
            "prev_track": lambda: self._step_track(-1),
            "next_track": lambda: self._step_track(1),
            "back": lambda: self._seek(-1),
            "forward": lambda: self._seek(1),
            "delete_track": self._delete_track,
            "effects": self._toggle_effects,
            "slow": lambda: self._change_speed(-1),
            "fast": lambda: self._change_speed(1),
            "lower": lambda: self._change_pitch(-1),
            "raise": lambda: self._change_pitch(1),
            "speed_reset": self._reset_speed,
            "pitch_reset": self._reset_pitch,
            "join": self._join,
            "chat_focus": self._focus_message_edit,
            "send": self._send_message,
            "send_file": self._send_file,
            "settings": self.open_settings,
            "connect": self._on_connect_button,
            "mic": self._toggle_mic,
            "camera": self._toggle_camera,
        }
        handler = actions.get(action_id)
        if handler is not None:
            handler()

    # -- настройки ----------------------------------------------------------

    def open_settings(self) -> None:
        audio_before = dict(self.cfg["audio"])
        algo_before = self._stretch_algo()
        dialog = SettingsDialog(self.cfg, self, self.is_teacher)
        if dialog.exec() != QDialog.Accepted:
            return
        # Другой способ растяжения: посчитанные варианты сделаны прежним
        # и больше не годятся. Номер настройки помечаем заведомо чужим,
        # чтобы звук пересчитался заново, даже если скорость не менялась.
        if self._stretch_algo() != algo_before and self._effects_active():
            self._clear_effects_cache()
            self._effects_key = (0.0, 0.0)
            self._apply_effects()
        self._apply_volumes()
        # Новые сочетания клавиш действуют сразу.
        self._apply_shortcuts()
        # Размер буфера — общий для обеих сторон обмена, перезапуск не нужен.
        self.engine.set_buffer_preset(
            str(self.cfg["audio"].get("buffer", "normal")))
        # Компенсация задержки — тоже без перезапуска звука.
        self.engine.set_tx_delay(
            int(self.cfg["audio"].get("tx_delay_ms", 0) or 0))
        # Битрейт применяется к следующему кодировщику, поток не рвём.
        self.engine.set_opus_bitrate(
            int(self.cfg["audio"].get("opus_bitrate", 96) or 96))
        # Обработка первого входа (мониторинг, срез низа, компрессор)
        # включается без перезапуска звука: фильтр и компрессор живут в
        # состоянии потока, мониторинг читается на каждом блоке.
        self.engine.set_input_fx(
            monitor=bool(self.cfg["audio"].get("monitor1", False)),
            lowcut=bool(self.cfg["audio"].get("lowcut", False)),
            compressor=int(self.cfg["audio"].get("compressor", 0) or 0))
        # Новый шаг перемотки из настроек — на кнопках плеера.
        self._update_player_buttons()
        # Смена аудиоустройств применяется сразу: поток переоткрывается сам.
        device_keys = ("device1", "device2", "usb", "output",
                       "mode1", "mode2", "usb_mode", "driver",
                       "asio_device", "asio_in_channels", "asio_out_channels",
                       "device_buffer")
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
                _engine_midi_options(self.cfg["audio"]))
        appeared = self._refresh_volume_visibility()
        # Пишем, с чем именно сохранились настройки: по этому логу видно,
        # какой шаг перемотки и размер буфера выбраны, когда владелец
        # присылает отчёт о проблеме.
        audio = self.cfg.get("audio", {})
        runlog.write(
            "Настройки сохранены: драйвер {drv}, карта ASIO {card} "
            "({ain} вх. / {aout} вых.), буфер {b}, буфер драйвера {db}, "
            "шаг перемотки {s} с, "
            "растяжение {a}, задержка компенсации {d} мс, устройства "
            "{dev1} / {dev2} / "
            "{out}, режим входа {m1} / {m2} / {mu}, вход 1: мониторинг "
            "{mon}, срез низа {lc}, компрессор {comp}%, громкость метронома "
            "{m}, трека {p}.".format(
                drv=audio.get("driver", "wasapi"),
                card=audio.get("asio_device", ""),
                ain=int(audio.get("asio_in_channels", 0) or 0),
                aout=int(audio.get("asio_out_channels", 0) or 0),
                b=audio.get("buffer", "normal"),
                db=(f"{int(audio.get('device_buffer', 0) or 0)} отсчётов"
                    if int(audio.get("device_buffer", 0) or 0)
                    else "по драйверу"),
                s=(self.cfg.get("player", {}) or {}).get("skip", 5),
                a=self._stretch_algo(),
                d=int(audio.get("tx_delay_ms", 0) or 0),
                dev1=audio.get("device1", ""),
                dev2=audio.get("device2", ""),
                out=audio.get("output", ""),
                m1=audio.get("mode1", ""),
                m2=audio.get("mode2", ""),
                mu=audio.get("usb_mode", "ch1"),
                mon="вкл" if audio.get("monitor1") else "выкл",
                lc="вкл" if audio.get("lowcut") else "выкл",
                comp=int(audio.get("compressor", 0) or 0),
                m=(audio.get("volumes", {}) or {}).get("metronome", ""),
                p=(audio.get("volumes", {}) or {}).get("player", "")))
        message = "Настройки сохранены."
        if devices_changed and self.engine.running:
            message += " Звук перезапущен с новыми устройствами."
        if appeared:
            names = ", ".join(self.slider_titles[key] for key in appeared)
            message += f" На главном экране появился регулятор: {names}."
        if not self.is_teacher and self.cfg.get("name"):
            self.name_edit.setText(self.cfg["name"])
        self._set_status(message)

    def _confirm_close(self) -> bool:
        """Спрашивает перед закрытием во время урока.

        Случайное нажатие крестика не должно обрывать урок: закрываем
        только по явному выбору, а по умолчанию остаёмся на месте.
        """
        if self.is_teacher:
            if self.server is None or not self.connected:
                return True
            who = getattr(self.server, "active_name", "") or "ученик"
            text = (f"Сейчас идёт урок с {who}. Если закрыть программу, "
                    "урок закончится.")
        else:
            if self.client is None and not self._waiting_network:
                return True
            if self._waiting_network:
                text = ("Связь пропала, программа ждёт её возвращения. "
                        "Если закрыть, урок закончится.")
            else:
                text = ("Вы в кабинете у преподавателя. Если закрыть "
                        "программу, урок закончится.")
        box = QMessageBox(self)
        box.setWindowTitle("Закрыть программу?")
        box.setIcon(QMessageBox.Question)
        box.setText(text)
        box.setAccessibleName("Подтверждение закрытия программы")
        quit_button = box.addButton("Закрыть программу",
                                    QMessageBox.DestructiveRole)
        stay = box.addButton("Остаться", QMessageBox.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        announce(self.status_label, text + " Остаться — по умолчанию.")
        box.exec()
        return box.clickedButton() is quit_button

    def changeEvent(self, event) -> None:
        # Ушли из окна с зажатым правым Control — кнопку отпускания
        # программа уже не получит, поэтому микрофон возвращаем в mute
        # здесь же, иначе он останется включённым.
        if (event.type() == QEvent.Type.ActivationChange
                and not self.isActiveWindow()):
            self._ptt_off()
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        if not self._confirm_close():
            event.ignore()
            return
        self._want_connection = False
        self._stop_waiting()
        config.save(self.cfg)
        if self.is_teacher:
            # Урок мог идти в момент закрытия: сначала заметка о конце,
            # потом файл — она должна попасть в историю.
            self._lesson_finish()
            # История пишется и по ходу разговора, но при закрытии сохраняем
            # ещё раз: так в файле точно последнее состояние. Папка History
            # и файл ученика — только у преподавателя.
            student = self._history_student_name()
            if student:
                history.save(self._history, student)
        if self._shortcut_filter is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self._shortcut_filter)
            self._shortcut_filter = None
        self._stop_video()
        self.engine.stop()
        if self.server is not None:
            self.server.stop()
        if self.client is not None:
            self.client.stop()
        # Метка в журнале: если строки нет, а программа пропала из процессов,
        # значит закрылась она не сама — это видно по журналу и crash.log.
        runlog.write("Программа закрыта.")
        super().closeEvent(event)
