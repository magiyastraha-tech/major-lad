"""Аудиодвижок: захват, микс, UDP-передача и воспроизведение.

Один UDP-сокет обслуживает звук и видео:
  GCA1 + номер пакета (4 байта) + int16-отсчёты  — звук, 48 кГц, моно, 10 мс;
  GCV1 + номер кадра + номер фрагмента + всего   — видео, JPEG кусками;
  GCH1                                            — рукопожатие.

Звук идёт по UDP, потому что задержка важнее доставки: потерянный пакет
не пересылается, его место занимает следующий кадр. Для занятий это
правильный компромисс — пауза в 10 мс слышна меньше, чем накопление
задержки от повторов.
"""
from __future__ import annotations

import queue
import socket
import struct
import threading
import time

import numpy as np

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - без PortAudio модуль всё равно импортируется
    sd = None

from .metronome import Metronome
from .midi_synth import MidiInput, Synth

SAMPLE_RATE = 48000
BLOCK = 480          # 10 мс
UDP_PORT = 45801
MAGIC_AUDIO = b"GCA1"
MAGIC_HELLO = b"GCH1"
MAGIC_VIDEO = b"GCV1"


# ---------------------------------------------------------------------------
# Кольцевой буфер
# ---------------------------------------------------------------------------

class Ring:
    """Буфер между потоками: пишет звуковая карта, читает миксер."""

    def __init__(self, size: int):
        self._buf = np.zeros(size, dtype=np.float32)
        self._size = size
        self._write = 0
        self._read = 0
        self._count = 0
        self._lock = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        data = np.asarray(data, dtype=np.float32).reshape(-1)
        n = data.size
        if n == 0:
            return
        with self._lock:
            if n >= self._size:
                data = data[-self._size:]
                n = self._size
            overflow = self._count + n - self._size
            if overflow > 0:
                # Старое выбрасываем: лучше короткая задержка, чем растущая.
                self._read = (self._read + overflow) % self._size
                self._count -= overflow
            end = self._write + n
            if end <= self._size:
                self._buf[self._write:end] = data
            else:
                first = self._size - self._write
                self._buf[self._write:] = data[:first]
                self._buf[:end - self._size] = data[first:]
            self._write = end % self._size
            self._count += n

    def read(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        with self._lock:
            k = min(n, self._count)
            if k:
                end = self._read + k
                if end <= self._size:
                    out[:k] = self._buf[self._read:end]
                else:
                    first = self._size - self._read
                    out[:first] = self._buf[self._read:]
                    out[first:k] = self._buf[:k - first]
                self._read = end % self._size
                self._count -= k
        return out

    def clear(self) -> None:
        with self._lock:
            self._read = self._write = self._count = 0


# ---------------------------------------------------------------------------
# Источник звука
# ---------------------------------------------------------------------------

class Source:
    def __init__(self, key: str, device_indices: list[int] | int):
        if isinstance(device_indices, int):
            device_indices = [device_indices]
        self.key = key
        self.device_indices = list(device_indices)
        self.device_index = self.device_indices[0] if self.device_indices else -1
        self.ring = Ring(SAMPLE_RATE)
        self.volume = 1.0
        self.stream = None

    def open(self) -> None:
        # Не всякое устройство отдаёт один канал: моно-микрофоны и стерео-
        # интерфейсы ведут себя по-разному. Пробуем моно, потом стерео.
        # Индексы идут в порядке предпочтения драйверов (WASAPI первым):
        # если один драйвер не открылся, пробуем следующий.
        last_error: Exception | None = None
        for index in self.device_indices:
            self.device_index = index
            for channels in (1, 2):
                stream = None
                try:
                    stream = sd.InputStream(
                        device=index,
                        channels=channels,
                        samplerate=SAMPLE_RATE,
                        blocksize=BLOCK,
                        dtype="float32",
                        latency="low",
                        callback=self._callback,
                    )
                    stream.start()
                    self.stream = stream
                    return
                except Exception as exc:
                    last_error = exc
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
        raise last_error if last_error is not None else RuntimeError("вход не открыт")

    def _callback(self, indata, frames, time_info, status) -> None:
        self.ring.write(indata[:, 0].copy())

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.ring.clear()


# ---------------------------------------------------------------------------
# Устройства
# ---------------------------------------------------------------------------

def list_input_devices() -> list[str]:
    if sd is None:
        return []
    try:
        return [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
    except Exception:
        return []


def list_output_devices() -> list[str]:
    if sd is None:
        return []
    try:
        return [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
    except Exception:
        return []


# Одно и то же устройство Windows показывает под несколькими драйверами:
# MME, DirectSound, WASAPI, WDM-KS. Имя у них одинаковое, а задержка разная —
# MME тянет больше сотни миллисекунд, WASAPI в режиме низкой задержки даёт
# десятки. Поэтому из совпадений по имени выбираем WASAPI, а MME оставляем
# последним: он годится только если остальное не открылось.
HOSTAPI_PREFERENCE = ("wasapi", "wdm-ks", "directsound", "mme")


def _hostapi_rank(device: dict, hostapis: list) -> int:
    try:
        name = str(hostapis[device["hostapi"]]["name"]).lower()
    except Exception:
        name = ""
    for rank, pref in enumerate(HOSTAPI_PREFERENCE):
        if pref in name:
            return rank
    return len(HOSTAPI_PREFERENCE)


def find_devices(name: str, kind: str) -> list[int]:
    """Все устройства с этим именем, лучший драйвер первым.

    Имена переживают перезапуск, индексы — нет, поэтому в настройках хранится
    имя, а индекс ищем каждый раз заново.
    """
    if sd is None or not name:
        return []
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return []
    found: list[tuple[int, int]] = []
    for index, dev in enumerate(devices):
        if dev["name"] != name:
            continue
        if kind == "input" and dev["max_input_channels"] <= 0:
            continue
        if kind == "output" and dev["max_output_channels"] <= 0:
            continue
        found.append((_hostapi_rank(dev, hostapis), index))
    found.sort()
    return [index for _, index in found]


def find_device(name: str, kind: str) -> int | None:
    """Лучшее устройство с этим именем или None."""
    found = find_devices(name, kind)
    return found[0] if found else None


def _stream_latency(stream) -> float | None:
    """Задержка потока в секундах. У дуплексного потока она приходит парой."""
    try:
        value = stream.latency
        if isinstance(value, (tuple, list)):
            value = value[-1]
        return float(value)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Движок
# ---------------------------------------------------------------------------

class AudioEngine:
    def __init__(self, log=None, on_video=None):
        self.log = log or (lambda *a: None)
        self.on_video = on_video
        self.student = Ring(SAMPLE_RATE)
        self.sources: dict[str, Source] = {}
        self.volumes = {"student": 0.8, "in1": 1.0, "in2": 1.0,
                        "usb": 1.0, "midi": 0.8, "metronome": 0.6}
        self.synth: Synth | None = None
        self.midi: MidiInput | None = None
        self.metronome = Metronome(SAMPLE_RATE)
        self._out = None
        self._out_channels = 1
        self._sock: socket.socket | None = None
        self._peer = None
        self._server_addr = None
        self._is_server = False
        self._send_q: queue.Queue = queue.Queue(maxsize=8)
        self._seq = 0
        self._running = False

    # -- запуск -------------------------------------------------------------

    def start(self, is_server: bool, server_addr=None, devices=None,
              output: str = "", midi_enabled: bool = False,
              midi_port: str = "") -> tuple[bool, str]:
        self.stop()
        if sd is None:
            return False, "Не установлен модуль sounddevice — звук недоступен."

        self._is_server = bool(is_server)
        self._server_addr = server_addr
        self._peer = None
        self._running = True

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            if is_server:
                sock.bind(("", UDP_PORT))
            else:
                sock.bind(("", 0))
            sock.settimeout(0.5)
        except OSError as exc:
            self._running = False
            return False, f"Не удалось открыть UDP-порт: {exc}"
        self._sock = sock
        threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()

        out_indices = find_devices(output, "output") if output else []
        if not out_indices:
            try:
                default = sd.default.device[1]
                if isinstance(default, int) and default >= 0:
                    out_indices = [default]
            except Exception:
                out_indices = []
        # Звуковая карта может не принимать моно: тогда открываем стерео
        # и дублируем сигнал в оба канала. Драйверы перебираем от быстрого
        # к медленному — WASAPI первым, MME последним.
        out_error: Exception | None = None
        for index in out_indices:
            for channels in (1, 2):
                stream = None
                try:
                    stream = sd.OutputStream(
                        device=index,
                        channels=channels,
                        samplerate=SAMPLE_RATE,
                        blocksize=BLOCK,
                        dtype="float32",
                        latency="low",
                        callback=self._out_callback,
                    )
                    stream.start()
                    self._out = stream
                    self._out_channels = channels
                    latency = _stream_latency(stream)
                    detail = f", задержка {latency:.3f} с" if latency is not None else ""
                    self.log(f"Аудиовыход открыт: {output or index}, "
                             f"{channels} кан.{detail}")
                    break
                except Exception as exc:
                    out_error = exc
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
            if self._out is not None:
                break
        if self._out is None:
            self.stop()
            return False, f"Не удалось открыть аудиовыход: {out_error}"
        latency = _stream_latency(self._out)
        if latency is not None and latency > 0.06:
            self.log(f"Внимание: задержка вывода {latency:.3f} с — "
                     f"похоже, выбран медленный драйвер (MME).")

        for key, name in (devices or {}).items():
            if not name:
                continue
            indices = find_devices(name, "input")
            if not indices:
                self.log(f"Вход не найден: {name}")
                continue
            source = Source(key, indices)
            try:
                source.open()
                self.sources[key] = source
            except Exception as exc:
                self.log(f"Не удалось открыть вход «{name}»: {exc}")

        if midi_enabled:
            self.set_midi(True, midi_port)

        if latency is not None:
            return True, f"Звук запущен. Задержка вывода {latency * 1000:.0f} мс."
        return True, "Звук запущен."

    def set_midi(self, enabled: bool, port: str = "") -> None:
        """Включает или выключает MIDI-вход на работающем движке.

        Нужно, чтобы клавиатура начинала играть сразу после сохранения
        настроек, без закрытия кабинета.
        """
        if self.midi is not None:
            self.midi.close()
            self.midi = None
        if not enabled:
            self.synth = None
            return
        if self.synth is None:
            self.synth = Synth(SAMPLE_RATE)
        midi = MidiInput(self.synth, self.log)
        if not midi.open(port):
            self.log("MIDI-устройство не открыто.")
            return
        self.midi = midi

    def set_metronome(self, enabled: bool, bpm: int = 90, beats: int = 4) -> None:
        self.metronome.configure(enabled, bpm, beats)

    @property
    def running(self) -> bool:
        """Открыт ли звуковой поток прямо сейчас."""
        return self._running

    # -- остановка ----------------------------------------------------------

    def stop(self) -> None:
        self._running = False
        if self._out is not None:
            try:
                self._out.stop()
                self._out.close()
            except Exception:
                pass
            self._out = None
        for source in self.sources.values():
            source.close()
        self.sources.clear()
        if self.midi is not None:
            self.midi.close()
            self.midi = None
        self.synth = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._peer = None
        self.student.clear()
        while True:
            try:
                self._send_q.get_nowait()
            except queue.Empty:
                break

    # -- громкость ----------------------------------------------------------

    def set_volume(self, key: str, value_0_100: int) -> None:
        level = max(0, min(100, int(value_0_100))) / 100.0
        self.volumes[key] = level
        source = self.sources.get(key)
        if source is not None:
            source.volume = level

    # -- микшер -------------------------------------------------------------

    def _out_callback(self, outdata, frames, time_info, status) -> None:
        # Синтезатор рендерится ровно один раз: у него внутри фаза и огибающие,
        # второй вызов съел бы часть звука.
        synth = self.synth
        synth_out = None
        if synth is not None:
            try:
                synth_out = synth.render(frames)
            except Exception:
                synth_out = None
        midi_level = self.volumes.get("midi", 0.8)
        metro_level = self.volumes.get("metronome", 0.6)
        clicks = self.metronome.render(frames) * metro_level

        # В наушники: собеседник, своя MIDI-клавиатура и свой метроном.
        # Источники со входов сюда не идут — иначе микрофон поймает колонки
        # и получится эхо.
        local = self.student.read(frames) * self.volumes["student"]
        if synth_out is not None:
            local += synth_out * midi_level
        local += clicks
        np.clip(local, -1.0, 1.0, out=local)
        # Моно и стерео: в стерео потоке сигнал идёт в оба канала.
        outdata[:] = local[:, None]

        # Собеседнику: входы, MIDI и метроном — всё, что он должен слышать.
        mix = clicks.copy()
        for source in self.sources.values():
            block = source.ring.read(frames)
            if source.volume > 0:
                mix += block * source.volume
        if synth_out is not None:
            mix += synth_out * midi_level

        pcm = (np.clip(mix, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        try:
            self._send_q.put_nowait(pcm)
        except queue.Full:
            pass

    # -- сеть ---------------------------------------------------------------

    def _udp_loop(self) -> None:
        sock = self._sock
        while self._running and sock is not None:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                continue

            magic = data[:4]
            if magic == MAGIC_HELLO:
                self._peer = addr if self._is_server else self._server_addr
                if self._is_server:
                    try:
                        sock.sendto(MAGIC_HELLO, addr)
                    except OSError:
                        pass
                continue

            # Любой пакет от собеседника подтверждает его адрес.
            if self._peer is None and magic in (MAGIC_AUDIO, MAGIC_VIDEO):
                self._peer = addr

            if magic == MAGIC_AUDIO:
                pcm = np.frombuffer(data[8:], dtype=np.int16)
                self.student.write(pcm.astype(np.float32) / 32768.0)
            elif magic == MAGIC_VIDEO and self.on_video is not None:
                self.on_video(data)

    def _send_loop(self) -> None:
        sock = self._sock
        hello_at = 0.0
        while self._running and sock is not None:
            if not self._is_server and self._peer is None and self._server_addr:
                now = time.monotonic()
                if now - hello_at > 1.0:
                    hello_at = now
                    try:
                        sock.sendto(MAGIC_HELLO, self._server_addr)
                    except OSError:
                        pass
            try:
                pcm = self._send_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._peer is None:
                continue
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            packet = MAGIC_AUDIO + struct.pack("!I", self._seq) + pcm
            try:
                sock.sendto(packet, self._peer)
            except OSError:
                pass

    def send_video_packet(self, packet: bytes) -> None:
        """Вызывается видеопотоком; сокет общий со звуком."""
        sock = self._sock
        peer = self._peer
        if sock is None or peer is None:
            return
        try:
            sock.sendto(packet, peer)
        except OSError:
            pass
