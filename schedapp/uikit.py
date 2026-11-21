"""Озвучивание для программ экранного доступа.

Экранный диктор (NVDA, JAWS) сам читает то, что происходит с фокусом: как
называется кнопка, какая строка списка выбрана. Но бывают события, которые
фокус не меняют — «урок записан», «напоминание за час». Их диктор не
прочитает, пока ему не сказать отдельно. Этим и занимается Announcer:
он отправляет Windows событие «объявление», и диктор читает текст, не
трогая фокус и не уводя тебя с того места, где ты стоишь.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import QLabel, QWidget

try:  # Qt 6.8 и новее умеют объявления напрямую
    from PySide6.QtGui import QAccessibleAnnouncementEvent
except ImportError:  # старше — объявляем через смену имени
    QAccessibleAnnouncementEvent = None


class Announcer(QLabel):
    """Невидимая метка, которая говорит текст диктору.

    Текст ставится в очередь: если объявлений пришло два подряд, диктор
    прочитает оба по порядку, а не оборвёт первое на середине. Отдельная
    очередь нужна ещё и потому, что Windows не любит, когда её дёргают
    прямо из обработчика события — отсюда отложенная отправка.
    """

    _queued = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("announcer")
        self.setAccessibleName("Объявления программы")
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVisible(True)
        self.resize(1, 1)
        self._queue: list[tuple[str, bool]] = []
        self._busy = False
        self._queued.connect(self._collect, Qt.ConnectionType.QueuedConnection)

    # --- наружу -----------------------------------------------------------

    def speak(self, text: str, assertive: bool = False) -> None:
        """Сказать текст. assertive — перебить текущую речь (важное)."""
        text = (text or "").strip()
        if not text:
            return
        self._queued.emit(text, assertive)

    # --- внутри -----------------------------------------------------------

    def _collect(self, text: str, assertive: bool) -> None:
        self._queue.append((text, assertive))
        if not self._busy:
            self._flush()

    def _flush(self) -> None:
        if not self._queue:
            self._busy = False
            return
        self._busy = True
        text, assertive = self._queue.pop(0)
        self._deliver(text, assertive)
        # Даём диктору время проговорить, прежде чем слать следующее.
        QTimer.singleShot(900, self._flush)

    def _deliver(self, text: str, assertive: bool) -> None:
        if QAccessibleAnnouncementEvent is not None:
            try:
                event = QAccessibleAnnouncementEvent(self, text)
                event.setPoliteness(
                    QAccessible.AnnouncementPoliteness.Assertive
                    if assertive
                    else QAccessible.AnnouncementPoliteness.Polite)
                QAccessible.updateAccessibility(event)
                return
            except (AttributeError, TypeError):
                pass
        # Запасной путь: смена имени метки. Диктор читает новое имя,
        # если метка доступна и событие отправлено руками.
        self.setAccessibleName(text)
        QAccessible.updateAccessibility(
            QAccessibleEvent(self, QAccessible.Event.NameChanged))
        if assertive:
            QAccessible.updateAccessibility(
                QAccessibleEvent(self, QAccessible.Event.Alert))


def announce(widget: QWidget, text: str, assertive: bool = False) -> None:
    """Сказать текст через Announcer, который есть в окне widget."""
    text = (text or "").strip()
    if not text:
        return
    # Сначала окно виджета, потом его родители: диалог может быть дочерним
    # окном главного окна, и объявлять должен общий Announcer.
    seen = []
    current = widget
    while current is not None and current not in seen:
        seen.append(current)
        announcer = getattr(current, "announcer", None)
        if isinstance(announcer, Announcer):
            announcer.speak(text, assertive)
            return
        found = current.findChild(Announcer)
        if found is not None:
            found.speak(text, assertive)
            return
        current = current.parentWidget()
    # Совсем некуда — хотя бы имени виджета дадим текст: диктор прочитает
    # его при следующем заходе на элемент.
    widget.setAccessibleName(text)


class ShortcutFilter(QObject):
    """Перехват горячих клавиш на всё приложение.

    F2, F3, F4, F5 и F9 должны работать из любого окна программы, даже
    если фокус в поле ввода: иначе нажатие уйдёт в поле и ничего не
    случится.
    """

    def __init__(self, handler, parent: QObject | None = None):
        super().__init__(parent)
        self._handler = handler
        self._keys = {
            Qt.Key.Key_F2, Qt.Key.Key_F3, Qt.Key.Key_F4, Qt.Key.Key_F5,
            Qt.Key.Key_F9,
        }

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt)
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in self._keys and not (
                    event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier):
                self._handler(key)
                event.accept()
                return True
        return super().eventFilter(obj, event)
