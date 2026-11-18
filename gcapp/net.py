"""Канал управления: подключение, чат, файлы, состояние кабинета.

Звук и видео идут отдельно, по UDP (см. audio.py). Здесь — надёжный TCP:
рукопожатие, сообщения чата, передача файлов и очередь ожидания, когда
кабинет занят другим учеником.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import uuid
from pathlib import Path

from . import media

APP_PORT = 45800
UDP_PORT = 45801
# Сколько ждём решения преподавателя, если место занято. Дольше держать
# ученика в неведении нечестно: он лучше узнает ответ и попробует позже.
WAIT_DECISION = 120
SAVE_DIR = Path(__file__).resolve().parent.parent / "received"


def _chat_id() -> str:
    """Уникальный номер сообщения.

    По нему сообщение находят обе стороны: удалили у себя — у собеседника
    исчезает то же самое, а не соседнее.
    """
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Кадрирование сообщений: 4 байта длины + JSON в UTF-8.
# ---------------------------------------------------------------------------

def send_json(sock: socket.socket, obj: dict, lock: threading.Lock | None = None) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    packet = struct.pack("!I", len(data)) + data
    if lock is not None:
        with lock:
            sock.sendall(packet)
    else:
        sock.sendall(packet)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("соединение закрыто")
        buf.extend(chunk)
    return bytes(buf)


def recv_json(sock: socket.socket) -> dict:
    size = struct.unpack("!I", recv_exact(sock, 4))[0]
    return json.loads(recv_exact(sock, size).decode("utf-8"))


# ---------------------------------------------------------------------------
# Файлы
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    name = os.path.basename((name or "файл").replace("\\", "/"))
    name = name.replace("\x00", "").strip()
    return name or "файл"


def save_received(name: str, data: bytes) -> str:
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    path = SAVE_DIR / _safe_name(name)
    if path.exists():
        stem, suffix = path.stem, path.suffix
        i = 1
        while True:
            candidate = SAVE_DIR / f"{stem} ({i}){suffix}"
            if not candidate.exists():
                path = candidate
                break
            i += 1
    path.write_bytes(data)
    return str(path)


# ---------------------------------------------------------------------------
# Сервер (преподаватель)
# ---------------------------------------------------------------------------

class Server:
    """Кабинет преподавателя. Один активный ученик, остальные ждут."""

    def __init__(self, name: str, on_status=None, on_chat=None, on_file=None,
                 on_peer=None, on_join_request=None, on_video_state=None,
                 on_chat_delete=None, on_chat_edit=None,
                 port: int = APP_PORT):
        self.name = name
        self.port = port
        self.on_status = on_status or (lambda *a: None)
        self.on_chat = on_chat or (lambda *a: None)
        self.on_chat_delete = on_chat_delete or (lambda *a: None)
        self.on_chat_edit = on_chat_edit or (lambda *a: None)
        self.on_file = on_file or (lambda *a: None)
        self.on_peer = on_peer or (lambda *a: None)
        self.on_video_state = on_video_state or (lambda *a: None)
        # Второй ученик не входит молча и не висит в очереди: преподаватель
        # решает сам — заменить текущего или отказать.
        self.on_join_request = on_join_request or (lambda *a: None)
        self._sock: socket.socket | None = None
        self._active: "_Session | None" = None
        self._waiting: list["_Session"] = []
        self._lock = threading.Lock()
        self._running = False

    # -- жизненный цикл -----------------------------------------------------

    def start(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", self.port))
            s.listen(5)
        except OSError as exc:
            self.on_status(f"Не удалось открыть кабинет на порту {self.port}: {exc}")
            return False
        self._sock = s
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        self.on_status(f"Кабинет открыт. Порт {self.port}. Ожидание ученика.")
        return True

    def stop(self) -> None:
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        with self._lock:
            sessions = list(self._waiting)
            if self._active:
                sessions.append(self._active)
            # Кабинет закрыт: выходящие соединения уже не должны сообщать
            # «ученик отключился» — это перебило бы «Кабинет закрыт».
            self._active = None
            self._waiting.clear()
        # Сначала прощаемся, потом закрываем: иначе ученик видит только
        # оборванную связь и не понимает, что урок закончился.
        for session in sessions:
            session.end("closed")

    # -- очередь ------------------------------------------------------------

    def join(self, session: "_Session") -> bool:
        """Занять свободный кабинет. Если занят — False, без очереди."""
        with self._lock:
            if self._active is None:
                self._active = session
                return True
            return False

    @property
    def active_name(self) -> str:
        """Имя ученика, который сейчас в кабинете. Пусто — кабинет свободен."""
        with self._lock:
            return self._active.name if self._active is not None else ""

    def request_join(self, session: "_Session") -> None:
        """Спросить преподавателя, пускать ли второго ученика."""
        with self._lock:
            if session not in self._waiting:
                self._waiting.append(session)
        self.on_join_request(session)

    def cancel_wait(self, session: "_Session") -> None:
        with self._lock:
            if session in self._waiting:
                self._waiting.remove(session)

    def decide(self, session: "_Session", accept: bool) -> None:
        """Ответ преподавателя на второй вход."""
        with self._lock:
            still_waiting = session in self._waiting
        if not still_waiting or session._closed:
            # Пока решали, ученик отключился или истекло время ожидания.
            # Принимать его некуда — и выгонять за него текущего нельзя.
            self.on_status(f"{session.name} уже не ждёт ответа.")
            return
        self.cancel_wait(session)
        if not accept:
            session.refuse()
            self.on_status(f"{session.name} не подключён: идёт урок.")
            return
        with self._lock:
            previous = self._active
            self._active = session
        # Текущего ученика отпускаем отдельно: его собственный выход из
        # чата уже не тронет кабинет, место занято новым.
        if previous is not None:
            previous.end("replaced")
            self.on_peer(False)
        session.promote()

    def leave(self, session: "_Session") -> None:
        ask = None
        with self._lock:
            if session is self._active:
                self._active = None
                self.on_peer(False)
                # Кабинет освободился. Если кто-то уже просится, спрашиваем
                # преподавателя заново — теперь про свободное место.
                if self._waiting:
                    ask = self._waiting[0]
            elif session in self._waiting:
                self._waiting.remove(session)
        if ask is not None:
            self.on_join_request(ask)

    def current(self) -> "_Session | None":
        with self._lock:
            return self._active

    # -- сообщения ----------------------------------------------------------

    def send_chat(self, text: str) -> str | None:
        """Возвращает номер сообщения или None, если отправить не вышло."""
        session = self.current()
        if session is None:
            self.on_status("В кабинете никого нет — сообщение не отправлено.")
            return None
        mid = _chat_id()
        ok = session.send_json({"type": "chat", "id": mid,
                                "from": self.name, "text": text})
        return mid if ok else None

    def send_chat_delete(self, mid: str) -> bool:
        session = self.current()
        if session is None:
            return False
        return session.send_json({"type": "chat_delete", "id": mid})

    def send_chat_edit(self, mid: str, text: str) -> bool:
        session = self.current()
        if session is None:
            return False
        return session.send_json({"type": "chat_edit", "id": mid, "text": text})

    def send_file(self, path: str) -> bool:
        session = self.current()
        if session is None:
            self.on_status("В кабинете никого нет — файл не отправлен.")
            return False
        return session.send_file(path)

    def send_video_state(self, on: bool) -> bool:
        """Сообщаем ученику, что камера включается или гаснет.

        Без этого у него замирает последний кадр, и он не понимает,
        что камеру просто выключили."""
        session = self.current()
        if session is None:
            return False
        return session.send_json({"type": "video", "on": bool(on)})

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                return
            session = _Session(self, conn, addr)
            threading.Thread(target=session.run, daemon=True).start()


class _Session:
    """Одно подключение ученика."""

    def __init__(self, server: Server, conn: socket.socket, addr):
        self.server = server
        self.conn = conn
        self.addr = addr
        self.name = "Ученик"
        self._send_lock = threading.Lock()
        self._promoted = threading.Event()
        self._refused = threading.Event()
        self._closed = False

    def send_json(self, obj: dict) -> bool:
        try:
            send_json(self.conn, obj, self._send_lock)
            return True
        except OSError:
            self.close()
            return False

    def promote(self) -> None:
        self._promoted.set()
        if self.send_json({"type": "ready"}):
            self.server.on_peer(True)
            self.server.on_status(f"Ученик в кабинете: {self.name}")

    def refuse(self) -> None:
        """Вежливо отказываем: место занято."""
        self._refused.set()
        self.send_json({"type": "rejected"})
        self.close()

    def end(self, reason: str) -> None:
        """Сообщаем, что урок закончен, и закрываем канал."""
        self.send_json({"type": "ended", "reason": reason})
        self.close()

    def run(self) -> None:
        try:
            hello = recv_json(self.conn)
        except Exception:
            self.close()
            return

        self.name = (str(hello.get("name") or "Ученик")).strip()[:40] or "Ученик"

        if self.server.join(self):
            self.send_json({"type": "joined", "teacher": self.server.name})
            self.server.on_peer(True)
            self.server.on_status(f"Ученик в кабинете: {self.name}")
        else:
            # Место занято: спрашиваем преподавателя, а не ставим молча
            # в очередь. Ученик в это время видит «ждёт решения».
            self.send_json({"type": "waiting"})
            self.server.request_join(self)
            self.server.on_status(f"{self.name} просит подключиться.")
            deadline = time.monotonic() + WAIT_DECISION
            while time.monotonic() < deadline:
                if self._promoted.is_set():
                    break
                if self._refused.is_set():
                    return
                time.sleep(0.1)
            else:
                self.server.cancel_wait(self)
                self.refuse()
                return
            if self._closed:
                return

        self._chat_loop()

    def _chat_loop(self) -> None:
        while True:
            try:
                msg = recv_json(self.conn)
            except Exception:
                break
            kind = msg.get("type")
            if kind == "chat":
                self.server.on_chat(str(msg.get("from") or self.name),
                                    str(msg.get("text") or ""),
                                    str(msg.get("id") or _chat_id()))
            elif kind == "chat_delete":
                self.server.on_chat_delete(str(msg.get("id") or ""))
            elif kind == "chat_edit":
                self.server.on_chat_edit(str(msg.get("id") or ""),
                                         str(msg.get("text") or ""))
            elif kind == "file_start":
                size = int(msg.get("size") or 0)
                try:
                    data = recv_exact(self.conn, size)
                except Exception:
                    break
                path = save_received(str(msg.get("name") or "файл"), data)
                self.server.on_file(self.name, path)
                self.send_json({
                    "type": "chat",
                    "id": _chat_id(),
                    "from": "система",
                    "text": f"Файл получен: {os.path.basename(path)}",
                })
            elif kind == "media_list":
                # Ученик просит список треков из папки Media преподавателя.
                tracks = []
                for p in media.list_tracks():
                    try:
                        tracks.append({"name": p.name, "size": p.stat().st_size})
                    except OSError:
                        continue
                self.send_json({"type": "media_list", "tracks": tracks})
            elif kind == "media_get":
                # Ученик выбрал трек — отдаём его файлом, чтобы он лёг
                # в его собственную папку Media.
                name = str(msg.get("name") or "")
                path = next((p for p in media.list_tracks() if p.name == name),
                            None)
                if path is None:
                    self.send_json({"type": "media_error",
                                    "text": "Трек не найден."})
                else:
                    self.send_media(path)
            elif kind == "video":
                self.server.on_video_state(bool(msg.get("on")))
        self.server.leave(self)
        self.close()

    def send_file(self, path: str) -> bool:
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError:
            self.server.on_status("Не удалось прочитать файл.")
            return False
        try:
            send_json(self.conn, {"type": "file_start", "name": p.name,
                                  "size": len(data)}, self._send_lock)
            with self._send_lock:
                self.conn.sendall(data)
            return True
        except OSError:
            self.close()
            return False

    def send_media(self, path: str) -> bool:
        """Отправляет трек из папки Media. Пометка media говорит клиенту,
        что файл нужно положить в Media, а не в папку принятых файлов."""
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError:
            self.send_json({"type": "media_error",
                            "text": "Не удалось прочитать трек."})
            return False
        try:
            send_json(self.conn, {"type": "file_start", "name": p.name,
                                  "size": len(data), "media": True},
                      self._send_lock)
            with self._send_lock:
                self.conn.sendall(data)
            return True
        except OSError:
            self.close()
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Клиент (ученик)
# ---------------------------------------------------------------------------

class Client:
    """Клиент ученика: подключается по сохранённому адресу и ждёт кабинет."""

    def __init__(self, name: str, host: str, port: int = APP_PORT,
                 on_status=None, on_chat=None, on_file=None,
                 on_ready=None, on_busy=None, on_peer=None,
                 on_ended=None, on_media_list=None, on_media_saved=None,
                 on_video_state=None, on_chat_delete=None, on_chat_edit=None):
        self.name = name
        self.host = host
        self.port = port
        self.on_status = on_status or (lambda *a: None)
        self.on_chat = on_chat or (lambda *a: None)
        self.on_chat_delete = on_chat_delete or (lambda *a: None)
        self.on_chat_edit = on_chat_edit or (lambda *a: None)
        self.on_file = on_file or (lambda *a: None)
        self.on_ready = on_ready or (lambda *a: None)
        self.on_busy = on_busy or (lambda *a: None)
        self.on_peer = on_peer or (lambda *a: None)
        # Код окончания: closed — кабинет закрыли, replaced — подключили
        # другого ученика, rejected — не пустили, lost — связь оборвалась.
        self.on_ended = on_ended or (lambda *a: None)
        # Список треков преподавателя и трек, сохранённый в свою папку Media.
        self.on_media_list = on_media_list or (lambda *a: None)
        self.on_media_saved = on_media_saved or (lambda *a: None)
        # Камера собеседника включилась или погасла.
        self.on_video_state = on_video_state or (lambda *a: None)
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._connected = False
        # Выход по кнопке «Отключиться»: это не окончание урока, и объявлять
        # о нём как о разрыве связи не нужно.
        self._user_closed = False

    def connect(self) -> bool:
        self.disconnect()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=6)
        except OSError as exc:
            self.on_status(f"Не удалось подключиться к {self.host}: {exc}")
            self.on_peer(False)
            return False

        self._sock = sock
        # Тайм-аут шесть секунд нужен только на подключение и обмен
        # приветствием. Дальше канал живёт в обычном блокирующем режиме:
        # с оставшимся тайм-аутом чтение обрывалось через шесть секунд тишины
        # в чате, хотя соединение было целым — ученика выкидывало из кабинета.
        try:
            send_json(sock, {"type": "hello", "name": self.name}, self._send_lock)
            answer = recv_json(sock)
        except Exception as exc:
            self.on_status(f"Сервер не ответил: {exc}")
            self.disconnect()
            return False
        self._keepalive(sock)
        sock.settimeout(None)

        if answer.get("type") in ("busy", "waiting"):
            self.on_busy()
            threading.Thread(target=self._wait_ready, daemon=True).start()
        else:
            self._connected = True
            self.on_peer(True)
            self.on_ready(str(answer.get("teacher") or "Преподаватель"))
            threading.Thread(target=self._read_loop, daemon=True).start()
        return True

    @staticmethod
    def _keepalive(sock: socket.socket) -> None:
        """Держим соединение живым и быстро узнаём, если оно умерло.

        Без этого замолчавший собеседник висит часами: TCP не замечает,
        что на другом конце никого нет.
        """
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            return
        # У Windows свои имена и своя форма вызова, у остальных — опции TCP.
        if os.name == "nt" and hasattr(socket, "SIO_KEEPALIVE_VALS"):
            try:
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
                return
            except OSError:
                pass
        for option, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5),
                              ("TCP_KEEPCNT", 3)):
            name = getattr(socket, option, None)
            if name is None:
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, name, value)
            except OSError:
                pass

    def _wait_ready(self) -> None:
        sock = self._sock
        reason = "lost"
        try:
            while True:
                answer = recv_json(sock)
                kind = answer.get("type")
                if kind == "ready":
                    break
                if kind == "rejected":
                    reason = "rejected"
                    break
                if kind == "ended":
                    reason = "closed"
                    break
            else:  # pragma: no cover - цикл выходит только через break
                pass
        except Exception:
            pass
        else:
            if reason == "lost":
                self._connected = True
                self.on_peer(True)
                self.on_ready("Преподаватель")
                self._read_loop()
                return
        user = self._user_closed
        self.disconnect()
        self.on_peer(False)
        if not user:
            self.on_ended(reason)

    def _read_loop(self) -> None:
        sock = self._sock
        reason = "lost"
        try:
            while True:
                msg = recv_json(sock)
                kind = msg.get("type")
                if kind == "chat":
                    sender = str(msg.get("from") or "Преподаватель")
                    text = str(msg.get("text") or "")
                    self.on_chat(sender, text, str(msg.get("id") or _chat_id()))
                elif kind == "chat_delete":
                    self.on_chat_delete(str(msg.get("id") or ""))
                elif kind == "chat_edit":
                    self.on_chat_edit(str(msg.get("id") or ""),
                                      str(msg.get("text") or ""))
                elif kind == "file_start":
                    size = int(msg.get("size") or 0)
                    data = recv_exact(sock, size)
                    if msg.get("media"):
                        # Трек из папки Media преподавателя: кладём его
                        # в свою Media, чтобы можно было сразу включить.
                        try:
                            path = media.import_track(
                                str(msg.get("name") or "трек"), data)
                        except OSError as exc:
                            self.on_status(f"Не удалось сохранить трек: {exc}")
                            continue
                        self.on_media_saved(str(path))
                    else:
                        path = save_received(str(msg.get("name") or "файл"),
                                             data)
                        self.on_file("Преподаватель", path)
                        self.on_chat("система",
                                     f"Файл получен: {os.path.basename(path)}",
                                     _chat_id())
                elif kind == "media_list":
                    tracks = [str(t.get("name") or "")
                              for t in (msg.get("tracks") or [])
                              if t.get("name")]
                    self.on_media_list(tracks)
                elif kind == "media_error":
                    self.on_status(str(msg.get("text") or "Трек не получен."))
                elif kind == "video":
                    self.on_video_state(bool(msg.get("on")))
                elif kind == "ended":
                    reason = str(msg.get("reason") or "closed")
                    break
        except Exception:
            pass
        finally:
            user = self._user_closed
            self.disconnect()
            self.on_peer(False)
            # Код окончания идёт последним: обработчик ставит финальный
            # статус, и местный звук не перебивает его своим сообщением.
            if not user:
                self.on_ended(reason)

    def send_chat(self, text: str) -> str | None:
        """Возвращает номер сообщения или None, если отправить не вышло."""
        sock = self._sock
        if sock is None or not self._connected:
            self.on_status("Нет соединения — сообщение не отправлено.")
            return None
        mid = _chat_id()
        try:
            send_json(sock, {"type": "chat", "id": mid,
                             "from": self.name, "text": text},
                      self._send_lock)
            return mid
        except OSError:
            self.disconnect()
            return None

    def send_chat_delete(self, mid: str) -> bool:
        sock = self._sock
        if sock is None or not self._connected:
            return False
        try:
            send_json(sock, {"type": "chat_delete", "id": mid}, self._send_lock)
            return True
        except OSError:
            self.disconnect()
            return False

    def send_chat_edit(self, mid: str, text: str) -> bool:
        sock = self._sock
        if sock is None or not self._connected:
            return False
        try:
            send_json(sock, {"type": "chat_edit", "id": mid, "text": text},
                      self._send_lock)
            return True
        except OSError:
            self.disconnect()
            return False

    def send_file(self, path: str) -> bool:
        sock = self._sock
        if sock is None or not self._connected:
            self.on_status("Нет соединения — файл не отправлен.")
            return False
        p = Path(path)
        try:
            data = p.read_bytes()
            send_json(sock, {"type": "file_start", "name": p.name,
                             "size": len(data)}, self._send_lock)
            with self._send_lock:
                sock.sendall(data)
            return True
        except OSError:
            self.disconnect()
            return False

    def request_media_list(self) -> bool:
        """Спросить у преподавателя, какие треки есть в его папке Media."""
        sock = self._sock
        if sock is None or not self._connected:
            self.on_status("Нет соединения — список треков не получен.")
            return False
        try:
            send_json(sock, {"type": "media_list"}, self._send_lock)
            return True
        except OSError:
            self.disconnect()
            return False

    def request_media(self, name: str) -> bool:
        """Попросить преподавателя прислать трек в свою папку Media."""
        sock = self._sock
        if sock is None or not self._connected:
            self.on_status("Нет соединения — трек не получен.")
            return False
        try:
            send_json(sock, {"type": "media_get", "name": name},
                      self._send_lock)
            return True
        except OSError:
            self.disconnect()
            return False

    def send_video_state(self, on: bool) -> bool:
        """Сообщить преподавателю, что камера включается или гаснет."""
        sock = self._sock
        if sock is None or not self._connected:
            return False
        try:
            send_json(sock, {"type": "video", "on": bool(on)}, self._send_lock)
            return True
        except OSError:
            self.disconnect()
            return False

    def stop(self) -> None:
        """Отключение по кнопке. Звонок об окончании урока не приходит."""
        self._user_closed = True
        self.disconnect()

    def disconnect(self) -> None:
        self._connected = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
