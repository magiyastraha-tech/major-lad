"""Канал управления: подключение, чат, файлы, состояние кабинета.

Звук и видео идут отдельно, по UDP (см. audio.py). Здесь — надёжный TCP:
рукопожатие, сообщения чата, передача файлов и очередь ожидания, когда
кабинет занят другим учеником.
"""
from __future__ import annotations

import json
import os
import select
import socket
import struct
import threading
from pathlib import Path

APP_PORT = 45800
UDP_PORT = 45801
SAVE_DIR = Path(__file__).resolve().parent.parent / "received"


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
                 on_peer=None, port: int = APP_PORT):
        self.name = name
        self.port = port
        self.on_status = on_status or (lambda *a: None)
        self.on_chat = on_chat or (lambda *a: None)
        self.on_file = on_file or (lambda *a: None)
        self.on_peer = on_peer or (lambda *a: None)
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
        for session in sessions:
            session.close()

    # -- очередь ------------------------------------------------------------

    def join(self, session: "_Session") -> bool:
        with self._lock:
            if self._active is None:
                self._active = session
                return True
            self._waiting.append(session)
            return False

    def leave(self, session: "_Session") -> None:
        promoted = None
        with self._lock:
            if session is self._active:
                self._active = None
                self.on_peer(False)
                if self._waiting:
                    promoted = self._waiting.pop(0)
                    self._active = promoted
            elif session in self._waiting:
                self._waiting.remove(session)
        if promoted is not None:
            promoted.promote()

    def current(self) -> "_Session | None":
        with self._lock:
            return self._active

    # -- сообщения ----------------------------------------------------------

    def send_chat(self, text: str) -> bool:
        session = self.current()
        if session is None:
            self.on_status("В кабинете никого нет — сообщение не отправлено.")
            return False
        return session.send_json({"type": "chat", "from": self.name, "text": text})

    def send_file(self, path: str) -> bool:
        session = self.current()
        if session is None:
            self.on_status("В кабинете никого нет — файл не отправлен.")
            return False
        return session.send_file(path)

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
            self.send_json({"type": "busy"})
            self.server.on_status(f"{self.name} ждёт: кабинет пока занят.")
            while not self._promoted.wait(0.5):
                try:
                    ready, _, _ = select.select([self.conn], [], [], 0)
                    if ready:
                        peek = self.conn.recv(1, socket.MSG_PEEK)
                        if not peek:
                            raise ConnectionError("клиент ушёл")
                except Exception:
                    self.server.leave(self)
                    self.close()
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
                    "from": "система",
                    "text": f"Файл получен: {os.path.basename(path)}",
                })
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
                 on_ready=None, on_busy=None, on_peer=None):
        self.name = name
        self.host = host
        self.port = port
        self.on_status = on_status or (lambda *a: None)
        self.on_chat = on_chat or (lambda *a: None)
        self.on_file = on_file or (lambda *a: None)
        self.on_ready = on_ready or (lambda *a: None)
        self.on_busy = on_busy or (lambda *a: None)
        self.on_peer = on_peer or (lambda *a: None)
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._connected = False

    def connect(self) -> bool:
        self.disconnect()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=6)
        except OSError as exc:
            self.on_status(f"Не удалось подключиться к {self.host}: {exc}")
            self.on_peer(False)
            return False

        self._sock = sock
        try:
            send_json(sock, {"type": "hello", "name": self.name}, self._send_lock)
            answer = recv_json(sock)
        except Exception as exc:
            self.on_status(f"Сервер не ответил: {exc}")
            self.disconnect()
            return False

        if answer.get("type") == "busy":
            self.on_busy()
            threading.Thread(target=self._wait_ready, daemon=True).start()
        else:
            self._connected = True
            self.on_peer(True)
            self.on_ready(str(answer.get("teacher") or "Преподаватель"))
            threading.Thread(target=self._read_loop, daemon=True).start()
        return True

    def _wait_ready(self) -> None:
        sock = self._sock
        try:
            while True:
                answer = recv_json(sock)
                if answer.get("type") == "ready":
                    break
        except Exception:
            self.on_status("Соединение с преподавателем потеряно.")
            self.disconnect()
            return
        self._connected = True
        self.on_peer(True)
        self.on_ready("Преподаватель")
        self._read_loop()

    def _read_loop(self) -> None:
        sock = self._sock
        try:
            while True:
                msg = recv_json(sock)
                kind = msg.get("type")
                if kind == "chat":
                    sender = str(msg.get("from") or "Преподаватель")
                    text = str(msg.get("text") or "")
                    self.on_chat(sender, text)
                elif kind == "file_start":
                    size = int(msg.get("size") or 0)
                    data = recv_exact(sock, size)
                    path = save_received(str(msg.get("name") or "файл"), data)
                    self.on_file("Преподаватель", path)
                    self.on_chat("система", f"Файл получен: {os.path.basename(path)}")
        except Exception:
            self.on_status("Соединение с преподавателем закрыто.")
        finally:
            self.disconnect()
            self.on_peer(False)

    def send_chat(self, text: str) -> bool:
        sock = self._sock
        if sock is None or not self._connected:
            self.on_status("Нет соединения — сообщение не отправлено.")
            return False
        try:
            send_json(sock, {"type": "chat", "from": self.name, "text": text},
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
