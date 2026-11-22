"""Аудиодвижок: захват, микс, UDP-передача и воспроизведение.

Один UDP-сокет обслуживает звук и видео:
  GCA1 + номер пакета (4 байта) + int16-отсчёты  — звук, 48 кГц, моно, 10 мс;
  GCA2                                            — то же стерео;
  GCA5 / GCA6 + номер пакета + данные Opus        — звук со сжатием, моно/стерео;
  GCV1 + номер кадра + номер фрагмента + всего   — видео, JPEG кусками;
  GCH1                                            — рукопожатие.

Звук идёт по UDP, потому что задержка важнее доставки: потерянный пакет
не пересылается, его место занимает следующий кадр. Для занятий это
правильный компромисс — пауза в 10 мс слышна меньше, чем накопление
задержки от повторов.
"""
from __future__ import annotations

import queue
import re
import socket
import struct
import threading
import time

import numpy as np

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - без PortAudio модуль всё равно импортируется
    sd = None

from . import opus_codec
from .config import DEFAULT_DEVICE
from .media import Player
from .metronome import Metronome
from .midi_synth import (MidiInput, Synth, list_midi_ports, pick_port,
                         port_key)

SAMPLE_RATE = 48000
BLOCK = 480          # 10 мс — единица буферов и пакетов
# Поток открываем коротким блоком: нажатие на MIDI-клавиатуре попадает в
# звук на границе блока, и чем блок короче, тем меньше эта пауза. 240
# отсчётов — 5 мс. Не всякий драйвер даёт такой блок, поэтому при отказе
# откатываемся на 10 мс.
AUDIO_BLOCK = 240

# Сколько звука держим в буфере. Это и есть задержка сверх драйвера.
# Рабочая цель — 4 блока, 40 мс: на локальной сети этого с запасом хватает
# на дрожание пакетов. Режем буфер только когда он вырос выше 8 блоков,
# 80 мс, и режем сразу до рабочей цели. Держать буфер ровно на минимуме
# нельзя: звуковая карта отдаёт данные пачками, и между пачками буфер
# успевает опустеть — в наушниках это слышно как хрипы и обрывки.
STUDENT_KEEP = BLOCK * 4
STUDENT_MAX = BLOCK * 8
INPUT_KEEP = BLOCK * 4
INPUT_MAX = BLOCK * 8

# Длина гашения на провале буфера: 5 мс. Дольше — уже слышно как затухание,
# короче — остаётся щелчок.
FADE_FRAMES = AUDIO_BLOCK

# Три набора буферов на выбор владельца. Низкий — минимальная задержка,
# но дрожание сети слышно сильнее; устойчивый — запас на плохую сеть.
BUFFER_PRESETS = {
    "low": (BLOCK * 2, BLOCK * 4),       # 20–40 мс
    "normal": (BLOCK * 4, BLOCK * 8),    # 40–80 мс
    "stable": (BLOCK * 8, BLOCK * 16),   # 80–160 мс
}
BUFFER_PRESET_NAMES = {
    "low": "минимальная задержка",
    "normal": "обычный",
    "stable": "устойчивый",
}


# Мягкий лимитер вместо жёсткого клиппинга. Жёсткий срез по единице — это
# цифровой перегруз: на слух он хрипит и щёлкает. До колена сигнал не
# трогаем совсем, выше — плавно поджимаем по tanh: громкое место остаётся
# плотным, но в потолок не упирается.
LIMIT_KNEE = 0.7


def soft_limit(x: np.ndarray) -> np.ndarray:
    """Поджимает пики выше 0.7, не давая сигналу дойти до 1.0."""
    if x.size == 0:
        return x
    ax = np.abs(x)
    peak = float(np.max(ax))
    if peak <= LIMIT_KNEE:
        return x
    over = np.clip((ax - LIMIT_KNEE) / (1.0 - LIMIT_KNEE), 0.0, None)
    shaped = LIMIT_KNEE + (1.0 - LIMIT_KNEE) * np.tanh(over)
    return np.where(ax > LIMIT_KNEE, np.sign(x) * shaped, x).astype(
        np.float32, copy=False)


def set_buffer_preset(name: str) -> str:
    """Переключает набор буферов. Возвращает применённое имя."""
    global STUDENT_KEEP, STUDENT_MAX, INPUT_KEEP, INPUT_MAX
    keep, maximum = BUFFER_PRESETS.get(name, BUFFER_PRESETS["normal"])
    if name not in BUFFER_PRESETS:
        name = "normal"
    STUDENT_KEEP = INPUT_KEEP = keep
    STUDENT_MAX = INPUT_MAX = maximum
    return name


# Запас входному буферу сверх пресета. Нужен, когда вывод открыт на медленном
# драйвере: тот зовёт обратный вызов пачками, и каждый вызов забирает из
# микрофонного кольца свой блок. Пачка вычерпывает кольцо досуха, в отправку
# уходит тишина — у собеседника это слышно как треск.
_EXTRA_KEEP = 0


def set_input_reserve(frames: int) -> None:
    """Добавляет входному буферу запас сверх пресета (в отсчётах)."""
    global _EXTRA_KEEP
    _EXTRA_KEEP = max(0, int(frames))


def input_bounds() -> tuple[int, int]:
    """Рабочая и предельная глубина входного буфера с учётом запаса."""
    return INPUT_KEEP + _EXTRA_KEEP, INPUT_MAX + _EXTRA_KEEP


# Запас приёмному буферу (звук собеседника). Причина та же пачка вызовов, но
# микрофонное кольцо наполняется тем же драйвером и потому дыр не даёт, а
# приёмное наполняется сетевыми пакетами ровно — пачка успевает его вычерпать.
# Поэтому запас здесь меряется самой задержкой вывода: сколько драйвер отдал
# разом, столько и надо переждать.
_RECV_RESERVE = 0


def set_recv_reserve(frames: int) -> None:
    """Добавляет приёмному буферу запас сверх пресета (в отсчётах)."""
    global _RECV_RESERVE
    _RECV_RESERVE = max(0, int(frames))


def student_bounds() -> tuple[int, int]:
    """Рабочая и предельная глубина приёмного буфера с учётом запаса."""
    return STUDENT_KEEP + _RECV_RESERVE, STUDENT_MAX + _RECV_RESERVE


UDP_PORT = 45801
MAGIC_AUDIO = b"GCA1"
# Стереозвук: два канала вперемешку, левый и правый. Отдельная метка, чтобы
# оба конца связи понимали, что в пакете не моно. Старый моноформат остаётся:
# программа, у которой все входы в режиме моно, шлёт по-прежнему GCA1 и не
# тратит трафик зря.
MAGIC_AUDIO_STEREO = b"GCA2"
MAGIC_HELLO = b"GCH1"
# Обмен возможностями: «я умею стерео». Шлётся отдельно от приветствия,
# чтобы связь со старой версией не рвалась: та просто не поймёт пакет и
# останется на моно, а мы не будем глушить её стереопотоком.
MAGIC_CAPS = b"GCC2"
# Сжатый звук. Опять отдельные метки: старый собеседник такой пакет не
# поймёт, поэтому Opus включается только после обмена возможностями.
MAGIC_AUDIO_OPUS = b"GCA5"
MAGIC_AUDIO_OPUS_STEREO = b"GCA6"
MAGIC_VIDEO = b"GCV1"

# Байт возможностей в пакете GCC2. Старая версия шлёт просто b"\x01" —
# значит, стерео она понимает, а сжатие нет.
CAPS_STEREO = 0x01
CAPS_OPUS = 0x02


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
        # Сколько отсчётов отдал последний read — по этому числу видно,
        # была ли в блоке дырка.
        self.last_got = 0
        # Статистика для журнала: сколько звука потеряно на переполнении
        # и сколько не хватило при чтении. По этим числам видно, хрипит
        # звук из-за сети, буфера или перегруза входа.
        self.overflows = 0
        self.underruns = 0
        # Подрезка буфера — это разрыв звука: выбрасываем кусок из середины.
        # cut_flag говорит читателю, что после этого отсчёта нужен плавный
        # вход, иначе в наушниках щёлкает.
        self.trims = 0
        self.cut_frames = 0
        self.cut_flag = False

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
                self.overflows += overflow
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
            if k < n:
                # Не хватило звука: остаток остаётся тишиной. Частые провалы
                # слышны как хрипы и щелчки.
                self.underruns += n - k
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
        self.last_got = k
        return out

    @property
    def level(self) -> int:
        """Сколько отсчётов сейчас лежит в кольце.

        Нужно, чтобы знать фактическую задержку звука: компенсация
        метронома должна опираться на то, что в буфере реально есть, а не
        на расчётную середину между keep и max.
        """
        with self._lock:
            return self._count

    def trim(self, keep: int, max_keep: int | None = None) -> None:
        """Держит буфер в разумных пределах, но не на самом минимуме.

        Буфер сглаживает неровности сети, разницу часов устройств и то, что
        драйвер отдаёт звук пачками. Если он копит больше нужного, задержка
        растёт и сама уже не падает: читаем ровно столько же, сколько
        приходит. Поэтому лишнее выбрасываем — но только когда его накопилось
        заметно больше цели: режем от max_keep до keep. Так работает
        гистерезис: обычные всплески буфер переживает молча, а не опускается
        каждый раз до голодного минимума, от которого в наушниках слышны
        хрипы. Без max_keep поведение прежнее — резать всё выше keep.
        """
        limit = keep if max_keep is None else max_keep
        with self._lock:
            self.cut_flag = False
            if self._count <= limit:
                return
            excess = self._count - keep
            self._read = (self._read + excess) % self._size
            self._count = keep
            if excess > 0:
                self.trims += 1
                self.cut_frames += excess
                self.cut_flag = True

    def clear(self) -> None:
        with self._lock:
            self._read = self._write = self._count = 0

    def reset_stats(self) -> None:
        with self._lock:
            self.overflows = 0
            self.underruns = 0
            self.trims = 0
            self.cut_frames = 0


class DelayLine:
    """Задержка сигнала, которую можно подтягивать на лету.

    Нужна, чтобы клик, MIDI и трек уходили собеседнику не раньше голоса:
    голос проходит микрофон и входной буфер и приходит позже, а сигналы,
    рождённые в самом микшере, уходят сразу. Задерживаем ровно их — и в
    потоке собеседника всё совпадает по времени.

    Заполнение входного буфера не постоянно: буфер подрезается на пороге и
    снова наполняется, а с медленным драйвером ещё и ходит ступеньками.
    Поэтому одной цифры на всю сессию не хватает — задержку надо подтягивать
    вслед за буфером. Резко сдвинуть читаемую позицию нельзя: это щелчок и
    проглоченный кусок. Поэтому задержка едет к цели понемногу, а чтение
    интерполируется между соседними отсчётами. На звуке шаг такой, что
    высота меняется на сотые доли процента; в паузах, где щёлкать нечему,
    можно двигаться быстрее и догнать цель сразу.
    """

    # Секунда с запасом на любую настройку.
    CAPACITY = SAMPLE_RATE
    # Сдвиг задержки за один отсчёт звука. 1/512 — это 0.2% скорости, около
    # трёх центов: на слух высота не меняется.
    SLEW = 1.0 / 512.0
    # В тишине можно быстрее: там нечего резать на стыке.
    SLEW_QUIET = 1.0 / 8.0

    def __init__(self) -> None:
        self._buf = np.zeros(self.CAPACITY, dtype=np.float32)
        self._write = 0
        self._delay = 0.0
        self._target = 0
        self._quiet = True
        # Прошёл ли через линию хоть какой-то звук. Пока не прошёл, задержку
        # можно ставить сразу: прыгать нечему.
        self._voiced = False

    @property
    def frames(self) -> int:
        return int(round(self._delay))

    def set_delay(self, frames: int) -> None:
        """Ставит задержку сразу.

        Так задают её при открытии вывода и при смене буфера или ручной
        компенсации: настройка должна действовать сейчас, а не подтягиваться
        полминуты. Плавно задержку двигает только set_target() в звуковом
        потоке — там резкий сдвиг был бы слышен постоянно.
        """
        frames = max(0, min(int(frames), self.CAPACITY - 8))
        self._target = frames
        self._delay = float(frames)

    def set_target(self, frames: int) -> None:
        """Новая цель на лету: к ней подтянемся в process()."""
        frames = max(0, min(int(frames), self.CAPACITY - 8))
        self._target = frames
        if not self._voiced:
            self._delay = float(frames)

    def process(self, data: np.ndarray) -> np.ndarray:
        data = np.asarray(data, dtype=np.float32).reshape(-1)
        n = data.size
        if n == 0:
            return data
        if n > self.CAPACITY // 2:
            # В звуковом потоке таких блоков не бывает, но если придёт —
            # режем на части, чтобы не потерять длину и выравнивание.
            half = self.CAPACITY // 2
            return np.concatenate([self.process(data[i:i + half])
                                   for i in range(0, n, half)])

        old = self._delay
        target = float(self._target)
        step = (self.SLEW_QUIET if self._quiet else self.SLEW) * n
        if old < target:
            new = min(target, old + step)
        elif old > target:
            new = max(target, old - step)
        else:
            new = old

        start = self._write
        # Вход пишем первым: при малой задержке читаются и только что
        # записанные отсчёты текущего блока.
        end = start + n
        if end <= self.CAPACITY:
            self._buf[start:end] = data
        else:
            first = self.CAPACITY - start
            self._buf[start:] = data[:first]
            self._buf[:n - first] = data[first:]
        self._write = end % self.CAPACITY
        self._delay = new

        if new <= 0.0 and old <= 0.0:
            self._quiet = float(np.max(np.abs(data))) < 1e-4
            if not self._quiet:
                self._voiced = True
            return data.copy()

        # Задержка едет по блоку ровно: последний отсчёт блока стоит там же,
        # где встанет первый отсчёт следующего, — стыка не слышно.
        ramp = old + (new - old) * (np.arange(n, dtype=np.float64) / n)
        pos = (start + np.arange(n, dtype=np.float64)
               - np.maximum(ramp, 1.0)) % self.CAPACITY
        i0 = pos.astype(np.int64)
        frac = (pos - i0).astype(np.float32)
        i1 = (i0 + 1) % self.CAPACITY
        out = self._buf[i0] * (1.0 - frac) + self._buf[i1] * frac
        # Быстро двигаемся на следующем блоке, только если этот был тихим:
        # так старт догоняет цель за доли секунды, а на звуке шаг остаётся
        # незаметным.
        level = float(np.max(np.abs(out)))
        self._quiet = level < 1e-4
        if not self._quiet:
            self._voiced = True
        return out


# ---------------------------------------------------------------------------
# Источник звука
# ---------------------------------------------------------------------------

class Source:
    """Один вход: устройство, режим канала и кольцевые буферы.

    Режимы:
      «ch1»    — моно с первого входа устройства;
      «ch2»    — моно со второго входа (нужно стереоустройство);
      «stereo» — оба входа сразу: первый слева, второй справа.

    Стереоисточник держит два буфера; моноблок дублируется в оба канала
    микшера. Так ученику можно отдать гитару слева, а голос справа.
    """

    def __init__(self, key: str, device_indices: list[int] | int,
                 mode: str = "ch1"):
        if isinstance(device_indices, int):
            device_indices = [device_indices]
        self.key = key
        self.mode = mode if mode in ("ch1", "ch2", "stereo") else "ch1"
        self.device_indices = list(device_indices)
        self.device_index = self.device_indices[0] if self.device_indices else -1
        self.ring = Ring(SAMPLE_RATE)
        self.ring_r = Ring(SAMPLE_RATE) if self.mode == "stereo" else None
        # Последний отсчёт прошлого блока — от него гасим провал буфера.
        self._last_sample = 0.0
        self.volume = 1.0
        self.stream = None
        self.channels = 1
        self.block = BLOCK
        # Задержка входного потока, как её отдаёт драйвер. Меряем один раз
        # при открытии: в звуковом потоке спрашивать её каждый блок незачем.
        self.latency: float | None = None
        # Задержка входа по отметкам самого драйвера. У WASAPI паспортная
        # нередко равна нулю, а отметки точные; сглаживается в callback.
        self.measured_latency: float | None = None
        # Уровень входа за последний промежуток: максимальный отсчёт и
        # сколько блоков упёрлось в предел. Перегруз на входе звучит как
        # хрип ещё до передачи, и никакая сеть тут не виновата.
        self.peak = 0.0
        self.clipped = 0
        # Сколько отсчётов вход принял всего. По приросту видно, живой ли
        # микрофон: устройство может открыться и молчать, а в настройках
        # оно выглядит рабочим.
        self.frames_in = 0
        # Сколько раз драйвер сообщил о переполнении входа: звук пришёл
        # быстрее, чем мы его забрали, и часть потерялась.
        self.status_overflows = 0

    @property
    def stereo(self) -> bool:
        return self.ring_r is not None

    def open(self) -> None:
        # Не всякое устройство отдаёт нужное число каналов: моно-микрофоны
        # и стереоинтерфейсы ведут себя по-разному. Второму каналу и стерео
        # нужно стереоустройство; если его нет, откатываемся на первый вход,
        # чтобы звук был хоть какой-то.
        # Индексы идут в порядке предпочтения драйверов (WASAPI первым):
        # если один драйвер не открылся, пробуем следующий.
        want = 1 if self.mode == "ch1" else 2
        choices = [want] if want == 1 else [2, 1]
        # Задержки драйверов — заранее, в настраивающем потоке: из
        # аудиопотока PortAudio и COM спрашивать нельзя.
        _prewarm_hostapi(self.device_indices)
        last_error: Exception | None = None
        for index in self.device_indices:
            self.device_index = index
            for channels in choices:
                for block in (AUDIO_BLOCK, BLOCK):
                    stream = None
                    try:
                        stream = sd.InputStream(
                            device=index,
                            channels=channels,
                            samplerate=SAMPLE_RATE,
                            blocksize=block,
                            dtype="float32",
                            latency="low",
                            callback=self._callback,
                        )
                        stream.start()
                        self.stream = stream
                        self.channels = channels
                        self.block = block
                        self.latency = _stream_latency(stream)
                        if channels == 1 and self.ring_r is not None:
                            # Стерео просили, а устройство моно: правый канал
                            # будет копией левого.
                            self.ring_r = None
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
        self.frames_in += frames
        try:
            if status and getattr(status, "input_overflow", False):
                self.status_overflows += 1
        except Exception:
            pass
        # Задержку входа берём из отметок драйвера: паспортная у WASAPI
        # бывает нулём, и тогда голос у собеседника обгоняет клик ровно на
        # незамеренную задержку устройства. Сглаживаем — отметки дрожат на
        # размер блока.
        value = _stamp_latency(time_info, "in")
        if value is not None:
            if self.measured_latency is None:
                self.measured_latency = value
            else:
                self.measured_latency += (value - self.measured_latency) * 0.1
        try:
            peak = float(np.max(np.abs(indata)))
            if peak > self.peak:
                self.peak = peak
            if peak >= 0.999:
                self.clipped += 1
        except Exception:
            pass
        if self.ring_r is not None and indata.shape[1] > 1:
            self.ring.write(indata[:, 0].copy())
            self.ring_r.write(indata[:, 1].copy())
        elif self.mode == "ch2" and indata.shape[1] > 1:
            self.ring.write(indata[:, 1].copy())
        else:
            self.ring.write(indata[:, 0].copy())

    def _read_ring(self, ring: Ring, frames: int) -> np.ndarray:
        """Читает блок и мягко гасит дырку, если звука не хватило.

        Обрыв в тишину — это ступенька в сигнале, и слышна она щелчком,
        даже если сама тишина короткая. Провал лучше пропустить плавно:
        за 5 мс уходим от последнего отсчёта в ноль.
        """
        ring.trim(*input_bounds())
        out = ring.read(frames)
        got = ring.last_got
        if got < frames:
            start = out[got - 1] if got > 0 else self._last_sample
            fade = min(frames - got, FADE_FRAMES)
            if start and fade > 0:
                out[got:got + fade] = start * np.linspace(
                    1.0, 0.0, fade, dtype=np.float32)
        self._last_sample = float(out[got - 1]) if got > 0 else self._last_sample
        return out

    def read(self, frames: int) -> tuple[np.ndarray, np.ndarray]:
        """Очередной блок: левый и правый каналы. У моно они одинаковые."""
        left = self._read_ring(self.ring, frames)
        if self.ring_r is not None:
            return left, self._read_ring(self.ring_r, frames)
        return left, left

    def latency_now(self) -> float:
        """Задержка входа для компенсации, в секундах.

        Порядок доверия: паспортная и замеренная по отметкам — что больше,
        затем оценка по драйверу. Ноль здесь означал бы «мгновенно» и
        разъехался бы с настоящим трактом на всю задержку устройства.
        """
        values = [value for value in (self.latency, self.measured_latency)
                  if value is not None and value > 0]
        if values:
            return max(values)
        return _hostapi_latency(self.device_index) or 0.0

    def fill(self) -> int:
        """Сколько звука ждёт в буферах прямо перед чтением.

        Меряем после подрезки: если буфер перебрал, лишнее сейчас выбросят,
        и задерживать клик на выброшенное незачем.
        """
        self.ring.trim(*input_bounds())
        level = self.ring.level
        if self.ring_r is not None:
            self.ring_r.trim(*input_bounds())
            level = max(level, self.ring_r.level)
        return level

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.ring.clear()
        if self.ring_r is not None:
            self.ring_r.clear()


# ---------------------------------------------------------------------------
# Устройства
# ---------------------------------------------------------------------------

# Одно и то же устройство Windows показывает под несколькими драйверами:
# MME, DirectSound, WASAPI, WDM-KS. Имя у них одинаковое, а задержка разная —
# MME тянет больше сотни миллисекунд, WASAPI в режиме низкой задержки даёт
# десятки. Поэтому из совпадений по имени выбираем WASAPI, а MME оставляем
# последним: он годится только если остальное не открылось.
HOSTAPI_PREFERENCE = ("wasapi", "wdm-ks", "directsound", "mme")

# В настройках по умолчанию показываем только WASAPI. Если показать все
# драйверы, в списке одно и то же устройство стоит несколько раз под
# одинаковым именем, и человек выбирает первое попавшееся — обычно это MME
# со стомиллисекундной задержкой. Ученик так и попадал на 90 мс вместо 22.
SHOW_HOSTAPIS = ("wasapi",)


def _hostapi_name(device: dict, hostapis: list) -> str:
    try:
        return str(hostapis[device["hostapi"]]["name"]).lower()
    except Exception:
        return ""


def _listed_devices(kind: str, show_all: bool) -> list[str]:
    """Имена устройств для списков в настройках, без повторов."""
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return []
    names: list[str] = []
    for dev in devices:
        channels = (dev["max_input_channels"] if kind == "input"
                    else dev["max_output_channels"])
        if channels <= 0:
            continue
        if not show_all:
            api = _hostapi_name(dev, hostapis)
            if not any(pref in api for pref in SHOW_HOSTAPIS):
                continue
        name = str(dev["name"])
        if name not in names:
            names.append(name)
    if not names and not show_all:
        # WASAPI не увидел ни одного устройства — так бывает со старой
        # картой без нормального драйвера. Пустой список не оставляем.
        return _listed_devices(kind, True)
    return names


def list_input_devices(show_all: bool = False) -> list[str]:
    return _listed_devices("input", show_all)


def list_output_devices(show_all: bool = False) -> list[str]:
    return _listed_devices("output", show_all)


def _hostapi_slow(index: int) -> bool:
    """MME и DirectSound отдают звук пачками — им нужен блок побольше.

    WASAPI зовёт обратный вызов ровным потоком каждые 5 мс, и каждый вызов
    забирает 5 мс микрофона. Старые драйверы могут отдать два-три вызова
    подряд, и такой пачкой микрофонное кольцо вычерпывается досуха — в
    отправку уходит тишина, а у собеседника это слышно как треск.
    """
    if sd is None:
        return False
    try:
        dev = sd.query_devices(index)
        name = str(sd.query_hostapis(dev["hostapi"])["name"]).lower()
    except Exception:
        return False
    return "mme" in name or "directsound" in name


def _hostapi_rank(device: dict, hostapis: list) -> int:
    try:
        name = str(hostapis[device["hostapi"]]["name"]).lower()
    except Exception:
        name = ""
    for rank, pref in enumerate(HOSTAPI_PREFERENCE):
        if pref in name:
            return rank
    return len(HOSTAPI_PREFERENCE)


# Номер копии устройства в имени Windows: «Наушники (2- Vault Green)».
# Одно и то же устройство перечисляется под несколькими драйверами, и Windows
# добавляет номер, чтобы имена не совпадали. Номер меняется от запуска
# к запуску, поэтому сохранённое имя перестаёт находиться.
_DUP_NUMBER = re.compile(r"\(\s*\d+\s*-\s*")


def _device_key(name: str) -> str:
    """Имя устройства без номера копии — для сравнения."""
    return _DUP_NUMBER.sub("(", name or "").strip().lower()


# Windows обрезает имена устройств MME до 31 символа: «Динамики (Graphite 2
# Pro Wirele» — это то же устройство, что «Динамики (Graphite 2 Pro
# Wireless)» в WASAPI. Без сопоставления по началу имени сохранённое
# обрезанное имя находило только MME — и программа оставалась на медленном
# драйвере, хотя WASAPI-вариант был рядом.
_MME_NAME_LEN = 30


def _same_device(a: str, b: str) -> bool:
    """Одно и то же устройство, даже если одно из имён обрезано."""
    a_key = _device_key(a)
    b_key = _device_key(b)
    if a_key == b_key:
        return True
    short, long = (a_key, b_key) if len(a_key) <= len(b_key) else (b_key, a_key)
    return len(short) >= _MME_NAME_LEN and long.startswith(short)


def find_devices(name: str, kind: str) -> list[int]:
    """Все устройства с этим именем, лучший драйвер первым.

    Имена переживают перезапуск, индексы — нет, поэтому в настройках хранится
    имя, а индекс ищем каждый раз заново. Если точного имени нет, ищем по
    имени без номера копии: «Наушники (2- Vault Green)» находит
    «Наушники (Vault Green)» и наоборот.
    """
    if sd is None or not name:
        return []
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return []

    def collect(exact: bool) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        for index, dev in enumerate(devices):
            dev_name = str(dev["name"])
            if exact:
                if dev_name != name:
                    continue
            elif not _same_device(dev_name, name):
                continue
            if kind == "input" and dev["max_input_channels"] <= 0:
                continue
            if kind == "output" and dev["max_output_channels"] <= 0:
                continue
            found.append((_hostapi_rank(dev, hostapis), index))
        found.sort()
        return [index for _, index in found]

    return collect(True) or collect(False)


def find_device(name: str, kind: str) -> int | None:
    """Лучшее устройство с этим именем или None."""
    found = find_devices(name, kind)
    return found[0] if found else None


def output_candidates(name: str) -> list[int]:
    """Все выходные устройства по порядку предпочтения.

    Сначала выбранное в настройках, потом системное по умолчанию, потом все
    остальные выходы — быстрый драйвер раньше медленного.

    Перебирать все нужно потому, что «системное по умолчанию» на некоторых
    машинах не открывается: это HDMI без подключённого монитора, отключённая
    звуковая карта или драйвер, не принимающий нужную частоту. Раньше
    программа в таком случае оставалась совсем без звука, хотя рабочие
    устройства в системе были.
    """
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return []
    preferred: list[int] = []
    if name:
        preferred.extend(find_devices(name, "output"))
    try:
        default = int(sd.default.device[1])
    except Exception:
        default = -1
    if 0 <= default < len(devices):
        default_dev = devices[default]
        if default_dev["max_output_channels"] > 0:
            # Системное по умолчанию PortAudio отдаёт под тем драйвером,
            # который перечислен первым, — обычно это MME со задержкой
            # 90 мс, хотя то же устройство есть в WASAPI с 22 мс. Поэтому
            # берём имя устройства и ищем его под всеми драйверами: WASAPI
            # окажется первым.
            for index in find_devices(str(default_dev["name"]), "output"):
                if index not in preferred:
                    preferred.append(index)
            if default not in preferred:
                preferred.append(default)
    rest: list[tuple[int, int]] = []
    for index, dev in enumerate(devices):
        if dev["max_output_channels"] <= 0 or index in preferred:
            continue
        rest.append((_hostapi_rank(dev, hostapis), index))
    rest.sort()
    return preferred + [index for _, index in rest]


def default_input_candidates() -> list[int]:
    """Системный микрофон по умолчанию, быстрый драйвер первым."""
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        index = int(sd.default.device[0])
    except Exception:
        return []
    if not (0 <= index < len(devices)):
        return []
    if devices[index]["max_input_channels"] <= 0:
        return []
    found = find_devices(str(devices[index]["name"]), "input")
    return found or [index]


def device_name(index: int) -> str:
    """Имя устройства по индексу — для лога. Индекс сам по себе ничего
    не говорит, а имя владелец видит в настройках Windows."""
    if sd is None:
        return str(index)
    try:
        return str(sd.query_devices(index)["name"])
    except Exception:
        return str(index)


def _stream_latency(stream) -> float | None:
    """Задержка потока в секундах. У дуплексного потока она приходит парой."""
    try:
        value = stream.latency
        if isinstance(value, (tuple, list)):
            value = value[-1]
        return float(value)
    except Exception:
        return None


def _stamp_latency(time_info, direction: str) -> float | None:
    """Задержка тракта по отметкам драйвера.

    Для входа: сколько прошло от момента, когда драйвер снял первый отсчёт
    блока, до вызова callback. Для выхода: сколько пройдёт от вызова
    callback до момента, когда первый отсчёт блока зазвучит в устройстве.
    WASAPI отдаёт эти отметки точно, а stream.latency там нередко приходит
    нулём — без отметок компенсация считалась бы без задержки устройства,
    и клик у собеседника уезжал бы от голоса.

    Отметки бывают нулевыми или несогласованными, если драйвер их не
    поддерживает: такие значения отбрасываем и остаёмся при паспортной
    задержке.
    """
    try:
        now = float(time_info.currentTime)
        if direction == "in":
            value = now - float(time_info.inputBufferAdcTime)
        else:
            value = float(time_info.outputBufferDacTime) - now
    except Exception:
        return None
    # Ограничение отсекает мусор: первый вызов после открытия потока иногда
    # приходит с несогласованными отметками, и подставлять такую задержку
    # в компенсацию нельзя. Полсекунды заведомо больше любой живой задержки
    # входа и выхода. NaN сюда не пройдёт: сравнение с ним всегда ложно.
    if not 0.0 <= value <= 0.5:
        return None
    return value


# Типичная задержка драйвера, когда ни паспорт потока, ни отметки её не
# назвали. Ноль в таком случае — не «мгновенно», а «неизвестно», и брать его
# за правду значит заведомо разъехаться на десятки миллисекунд.
HOSTAPI_LATENCY = (
    ("mme", 0.120),
    ("directsound", 0.040),
    ("wasapi", 0.020),
    ("wdm-ks", 0.010),
    ("asio", 0.010),
)


# Задержка по драйверу нужна в каждом звуковом блоке, а sd.query_devices
# и sd.query_hostapis лезут в PortAudio, а на Windows — в COM. Из
# аудиопотока это нельзя: COM отвечает «вызвано из неверного потока»
# (0x8001010d), и звук рвётся. Поэтому спрашиваем один раз при запуске —
# _prewarm_hostapi — и дальше только читаем таблицу.
_HOSTAPI_CACHE: dict[int, float | None] = {}


def _hostapi_latency(index: int | None, refresh: bool = False) -> float | None:
    """Оценка задержки по драйверу устройства. Последний довод, когда
    точных чисел нет: MME медленнее всех, WASAPI в общей очереди — заметно
    быстрее.

    Из аудиопотока вызывается с refresh=False: только таблица, без
    обращений к PortAudio. Значение None тоже кэшируется — неизвестный
    драйвер не заставляет спрашивать снова и снова.
    """
    if sd is None or index is None:
        return None
    index = int(index)
    if index < 0:
        return None
    if not refresh and index in _HOSTAPI_CACHE:
        return _HOSTAPI_CACHE[index]
    value: float | None = None
    try:
        api = sd.query_hostapis(sd.query_devices(index)["hostapi"])
        name = str(api["name"]).lower()
        for part, latency in HOSTAPI_LATENCY:
            if part in name:
                value = latency
                break
    except Exception:
        value = None
    _HOSTAPI_CACHE[index] = value
    return value


def _prewarm_hostapi(indices) -> None:
    """Заполняет таблицу задержек заранее — до открытия потока.

    Вызывается из обычного потока настройки, где PortAudio и COM отвечают
    нормально. Дальше звуковой блок берёт готовое число.
    """
    for index in list(indices or []):
        try:
            _hostapi_latency(index, refresh=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Движок
# ---------------------------------------------------------------------------

class AudioEngine:
    def __init__(self, log=None, on_video=None):
        self.log = log or (lambda *a: None)
        self.on_video = on_video
        self.student = Ring(SAMPLE_RATE)
        # Правый канал собеседника. Заполняется, только когда он шлёт стерео;
        # для моно потока остаётся пустым, и правый канал берётся из левого.
        self.student_r = Ring(SAMPLE_RATE)
        self._recv_stereo = False
        # Собеседник подтвердил, что понимает стерео. Пока не подтвердил —
        # шлём моно, как раньше.
        self._peer_stereo = False
        # Собеседник умеет принимать Opus. Пока не подтвердил — шлём
        # несжатый звук: старая версия сжатый пакет не разберёт.
        self._peer_opus = False
        # Собеседник прислал свои возможности. Пока их нет, свои шлём
        # настойчивее: иначе сторона, услышавшая чужие раньше, замолкает и
        # вторая так и не узнаёт про Opus — звук идёт несжатым.
        self._peer_caps = False
        # Звук собеседника: до первого наполнения буфера молчим, а не
        # читаем по капле — иначе старт связи начинается с треска.
        self._peer_live = False
        self._peer_wait = 0.0
        # Разрыв в потоке собеседника: первый целый пакет после него
        # вводим плавно, иначе слышно щелчок.
        self._peer_gap = False
        # Последний отсчёт и остаток плавного входа для каждого канала
        # собеседника — состояние медленного читателя.
        self._peer_last = [0.0, 0.0]
        self._peer_fade = [0, 0]
        # Запас приёма: постоянный (от медленного драйвера) и добавка,
        # которую движок поднимает, если звук собеседника рвался, и потом
        # медленно отпускает.
        self._recv_base = 0
        self._recv_adapt = 0
        self._recv_starve = 0
        self._recv_clean_t = 0.0
        self._recv_note = ""
        self._recv_note_t = 0.0
        # Счётчики сбоев драйвера вывода.
        self._out_underflow = 0
        # Счётчики сбоев кодека: первый попадёт в журнал, остальные молча.
        self._opus_errors = 0
        self._opus_fail_logged = False
        self.sources: dict[str, Source] = {}
        # Две независимые громкости: что слышу я и что слышит собеседник.
        self.volumes = {"student": 0.8, "in1": 1.0, "in2": 1.0,
                        "usb": 1.0, "midi": 0.8, "metronome": 0.6,
                        "player": 0.8}
        self.volumes_peer = dict(self.volumes)
        self.mic_muted = False
        self.synth: Synth | None = None
        self.midi: MidiInput | None = None
        self.metronome = Metronome(SAMPLE_RATE)
        # Трек живёт вне аудиопотока: кнопки двигают позицию, callback читает.
        self.player = Player(SAMPLE_RATE)
        self._out = None
        self._out_channels = 1
        # Индекс устройства вывода и его задержка по отметкам драйвера.
        # Паспортная у WASAPI бывает нулём, а без неё компенсация не знает,
        # на сколько музыкант слышит клик позже, чем тот родился.
        self._out_index = -1
        self._out_measured: float | None = None
        self._sock: socket.socket | None = None
        self._peer = None
        self._server_addr = None
        self._is_server = False
        self._send_q: queue.Queue = queue.Queue(maxsize=4)
        self._seq = 0
        self._running = False
        # Поколение запуска. Каждый поток запоминает своё и выходит, как
        # только поколение сменилось: после перезапуска звука старые потоки
        # не должны ни читать очередь, ни отправлять в закрытый сокет.
        self._generation = 0
        self._udp_thread: threading.Thread | None = None
        self._send_thread: threading.Thread | None = None
        # Сторож: следит за обратными вызовами вывода. Если звук встал,
        # переоткрывает выход сам, без участия человека.
        self._watchdog: threading.Thread | None = None
        self._last_cb = 0.0
        self._last_reopen = 0.0
        self._start_args: dict | None = None
        self._out_frames = 0
        self._stat_frames_t0 = 0
        self._tx_errors = 0
        self._cb_errors = 0
        self.buffer_preset = "normal"
        # Счётчики для журнала: потери по сети, провалы буфера, перегруз.
        self._stat_t0 = 0.0
        self._stat_connected = False
        self._rx_packets = 0
        self._rx_lost = 0
        self._rx_ooo = 0
        self._rx_last_seq: int | None = None
        self._tx_packets = 0
        self._tx_dropped = 0
        self._tx_nopeer = 0
        self._tx_clip = 0
        # Чья сторона играет минус и метроном. «local» — моя, «peer» —
        # собеседника: тогда его поток уже содержит дорожку и клик, а свой
        # плеер и метроном молчат, чтобы не было рассинхрона.
        # Компенсация задержки. Голос проходит микрофон и входной буфер и
        # приходит позже, а клик, MIDI и трек рождаются в микшере и уходят
        # сразу. Задерживаем передающую копию этих сигналов на задержку
        # тракта, чтобы у собеседника они совпали с голосом. 0 — авто.
        self.tx_delay_ms = 0
        # Битрейт Opus в кбит/с. Он влияет только на исходящий поток:
        # собеседник декодирует то, что получил, и его настройка ничего не
        # меняет. 96 хватает для урока, 128–320 — если канал позволяет.
        self.opus_bitrate = 96
        self._tx_delay = DelayLine()
        self._tx_delay_auto = 0
        # Сглаженное заполнение входного буфера. Оно гуляет на размер блока
        # туда-сюда, а подтягивать задержку за каждым колебанием незачем:
        # важна средняя задержка голоса.
        self._tx_fill = 0.0
        # Составляющие компенсации последнего расчёта, в миллисекундах:
        # выход, вход, буфер и итог. Раз в минуту уходят в журнал, чтобы
        # расхождение можно было проверить по числам, а не на слух.
        self._tx_parts = (0.0, 0.0, 0.0, 0)
        self._tx_log_t = 0.0
        # Сколько отсчётов принял каждый вход: по приросту видно молчащий
        # микрофон.
        self._source_frames: dict[str, int] = {}
        # Настройки MIDI запоминаются, чтобы автоподхват знал, какой порт
        # ждать, когда клавиатуру включат уже после запуска программы.
        self._midi_enabled = False
        self._midi_port = ""
        self._midi_fixed = False
        self._midi_absent = ""
        self._midi_last_try = 0.0

    # -- запуск -------------------------------------------------------------

    def start(self, is_server: bool, server_addr=None, devices=None,
              modes=None, output: str = "", midi_enabled: bool = False,
              midi_port: str = "", midi_fixed_velocity: bool = False
              ) -> tuple[bool, str]:
        self.stop()
        if sd is None:
            return False, "Не установлен модуль sounddevice — звук недоступен."

        self._is_server = bool(is_server)
        self._server_addr = server_addr
        self._peer = None
        # Новое поколение потоков и свежая очередь: старые потоки выйдут
        # сами, а их неотправленные пакеты не попадут в новый поток.
        self._generation += 1
        self._send_q = queue.Queue(maxsize=4)
        self._start_args = dict(
            is_server=is_server, server_addr=server_addr,
            devices=dict(devices or {}), modes=dict(modes or {}),
            output=output, midi_enabled=midi_enabled, midi_port=midi_port,
            midi_fixed_velocity=midi_fixed_velocity)
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
        generation = self._generation
        self._udp_thread = threading.Thread(
            target=self._udp_loop, args=(generation,), daemon=True)
        self._udp_thread.start()
        self._send_thread = threading.Thread(
            target=self._send_loop, args=(generation,), daemon=True)
        self._send_thread.start()
        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, daemon=True)
            self._watchdog.start()

        if output and not find_devices(output, "output"):
            self.log(f"Устройство вывода «{output}» не найдено — ищу другое.")
        out_indices = output_candidates(output)
        # Задержки драйверов — заранее: звуковой блок спрашивать их не может.
        _prewarm_hostapi(out_indices)
        # Звуковая карта может не принимать моно: тогда открываем стерео
        # и дублируем сигнал в оба канала. Драйверы перебираем от быстрого
        # к медленному — WASAPI первым, MME последним. Если «низкая
        # задержка» не принимается, пробуем обычную.
        out_error: Exception | None = None
        tried: list[str] = []
        failed: set[tuple[str, str]] = set()
        opened_index = -1
        for index in out_indices:
            label = device_name(index)
            if label not in tried:
                tried.append(label)
            # Медленным драйверам сразу даём блок 10 мс: мельче они всё
            # равно отдают пачками, только чаще.
            blocks = (BLOCK, AUDIO_BLOCK) if _hostapi_slow(index) else (AUDIO_BLOCK, BLOCK)
            for channels in (1, 2):
                opened = False
                for block in blocks:
                    for latency_mode in ("low", None):
                        stream = None
                        try:
                            stream = sd.OutputStream(
                                device=index,
                                channels=channels,
                                samplerate=SAMPLE_RATE,
                                blocksize=block,
                                dtype="float32",
                                latency=latency_mode,
                                callback=self._out_callback,
                            )
                            stream.start()
                            self._out = stream
                            self._out_channels = channels
                            opened_index = index
                            self._out_index = index
                            self._out_measured = None
                            # Отсчёт для сторожа начинается с открытия:
                            # первые вызовы приходят не сразу.
                            self._last_cb = time.monotonic()
                            latency = _stream_latency(stream)
                            detail = f", задержка {latency:.3f} с" if latency is not None else ""
                            self.log(f"Аудиовыход открыт: {label}, "
                                     f"{channels} кан., блок {block}{detail}")
                            opened = True
                            break
                        except Exception as exc:
                            out_error = exc
                            # Один и тот же отказ повторяется на всех
                            # вариантах каналов и блоков — пишем его в лог
                            # один раз, иначе журнал забьётся.
                            mark = (label, str(exc)[:120])
                            if mark not in failed:
                                failed.add(mark)
                                self.log(f"Выход «{label}» не открылся: {exc}")
                            if stream is not None:
                                try:
                                    stream.close()
                                except Exception:
                                    pass
                    if opened:
                        break
                if opened:
                    break
            if self._out is not None:
                break
        if self._out is None:
            self.stop()
            available = ", ".join(tried) if tried else "нет ни одного выходного устройства"
            self.log(f"Аудиовыход не открылся. Пробовал: {available}. "
                     f"Ошибка: {out_error}")
            return False, f"Не удалось открыть аудиовыход: {out_error}"
        latency = _stream_latency(self._out)
        slow = _hostapi_slow(opened_index) or (latency is not None and latency > 0.06)
        # Медленный вывод зовёт обратный вызов пачками, и каждый вызов
        # забирает из микрофонного кольца свой блок. Даём входу лишние
        # 40 мс: голос уйдёт к собеседнику на столько позже, зато без дыр
        # и треска. Компенсация задержки ниже это учитывает.
        set_input_reserve(BLOCK * 4 if slow else 0)
        # Приёмному буферу запас нужен не меньше самой задержки вывода: пачка
        # вызовов тянется примерно на один буфер драйвера, и приёмное кольцо
        # должно её переждать. Иначе у собеседника рвётся именно приём —
        # в логах это «пустых буферов …, микрофон 0».
        if slow:
            recv = (int(round(latency * SAMPLE_RATE)) if latency is not None
                    else BLOCK * 8) + BLOCK * 2
            recv = max(BLOCK * 4, min(BLOCK * 32, recv))
        else:
            recv = 0
        set_recv_reserve(recv)
        # Запас приёма: постоянная часть — от медленного драйвера, добавка
        # живёт в движке и растёт, если поток собеседника всё равно рвётся.
        self._recv_base = recv
        self._recv_adapt = 0
        self._recv_starve = 0
        self._recv_clean_t = 0.0
        self._recv_note = ""
        self._recv_note_t = 0.0
        self._peer_live = False
        self._peer_wait = 0.0
        self._peer_gap = False
        self._peer_last = [0.0, 0.0]
        self._peer_fade = [0, 0]
        self._out_underflow = 0
        if slow:
            at = f" ({latency:.3f} с)" if latency is not None else ""
            self.log(f"Внимание: вывод открыт на медленном драйвере{at} — "
                     f"входу добавлен запас 40 мс, приёму "
                     f"{recv * 1000 // SAMPLE_RATE} мс, чтобы звук не рвался.")

        configured = {k: v for k, v in (devices or {}).items() if v}
        for key, name in configured.items():
            if name == DEFAULT_DEVICE:
                # Имя устройства на другом компьютере другое, а «системный
                # по умолчанию» есть всегда: его и открываем.
                indices = default_input_candidates()
                if indices:
                    self.log(f"Вход «{key}»: системный микрофон "
                             f"«{device_name(indices[0])}».")
            else:
                indices = find_devices(name, "input")
            if not indices and len(configured) == 1 and name != DEFAULT_DEVICE:
                # Единственный выбранный микрофон пропал из списка — лучше
                # открыть системный по умолчанию, чем остаться совсем без
                # входа: собеседник тогда не слышит вообще ничего.
                fallback = default_input_candidates()
                if fallback:
                    self.log(f"Вход «{name}» не найден — открываю системный "
                             f"микрофон «{device_name(fallback[0])}».")
                    indices = fallback
            if not indices:
                self.log("Вход не найден: "
                         + ("системный микрофон по умолчанию"
                            if name == DEFAULT_DEVICE else name))
                continue
            mode = (modes or {}).get(key, "ch1")
            source = Source(key, indices, mode)
            try:
                source.open()
                self.sources[key] = source
            except Exception as exc:
                self.log(f"Не удалось открыть вход «{name}»: {exc}")
                continue
            mode_label = {"ch1": "моно, канал 1",
                          "ch2": "моно, канал 2",
                          "stereo": "стерео"}.get(source.mode, source.mode)
            in_latency = _stream_latency(source.stream)
            in_detail = (f", задержка входа {in_latency:.3f} с"
                         if in_latency is not None else "")
            self.log(f"Вход открыт: {device_name(source.device_index)}, "
                     f"{source.channels} кан., {mode_label}, "
                     f"блок {source.block}{in_detail}")

        # Компенсация задержки считается по уже открытым устройствам:
        # до этого задержка входа неизвестна.
        delay = self._apply_tx_delay()
        if self.sources:
            how = ("авто, дальше по фактическому буферу"
                   if self.tx_delay_ms <= 0 else "вручную")
            out_ms, in_ms, fill_ms, _ = self._tx_parts
            note = "" if self._out_measured is not None else ", выход оценочный"
            self.log(f"Компенсация задержки: {delay} мс ({how}). "
                     f"Выход {out_ms:.0f} мс, вход {in_ms:.0f} мс, "
                     f"буфер {fill_ms:.0f} мс{note}.")
            # Вручную выставленное число со временем устаревает: тракт
            # изменился, а компенсация осталась. Ученик слышит клик мимо
            # игры. Говорим прямо, если ручное сильно расходится с расчётом.
            manual = int(self.tx_delay_ms)
            if manual > 0 and self._tx_delay_auto > 0:
                diff = manual - self._tx_delay_auto
                if abs(diff) > 30:
                    self.log(
                        f"Компенсация выставлена вручную: {manual} мс, а по "
                        f"задержке устройств выходит {self._tx_delay_auto} мс "
                        f"(разница {diff:+d} мс). Если метроном у собеседника "
                        f"не совпадает с игрой, поставь «Автоматически».")

        if midi_enabled:
            self.set_midi(True, midi_port, midi_fixed_velocity)

        if latency is not None:
            return True, f"Звук запущен. Задержка вывода {latency * 1000:.0f} мс."
        return True, "Звук запущен."

    def set_midi(self, enabled: bool, port: str = "",
                 fixed_velocity: bool = False) -> None:
        """Включает или выключает MIDI-вход на работающем движке.

        Нужно, чтобы клавиатура начинала играть сразу после сохранения
        настроек, без закрытия кабинета.
        """
        self._midi_enabled = bool(enabled)
        self._midi_port = port or ""
        self._midi_fixed = bool(fixed_velocity)
        self._midi_absent = ""
        if self.midi is not None:
            self.midi.close()
            self.midi = None
        if not enabled:
            self.synth = None
            return
        self._open_midi()

    def _open_midi(self) -> bool:
        """Открывает сохранённый порт. Клавиатуры нет — тихо ждёт её."""
        ports = list_midi_ports()
        name = pick_port(self._midi_port, ports)
        if not name:
            if ports and self._midi_port and self._midi_absent != self._midi_port:
                self._midi_absent = self._midi_port
                self.log(f"MIDI «{self._midi_port}» не подключена, жду.")
            return False
        if self.synth is None:
            self.synth = Synth(SAMPLE_RATE)
        self.synth.fixed_velocity = self._midi_fixed
        midi = MidiInput(self.synth, self.log)
        if not midi.open(name):
            self.log("MIDI-устройство не открыто.")
            return False
        self.midi = midi
        self._midi_absent = ""
        return True

    def ensure_midi(self) -> None:
        """Подхватывает клавиатуру при подключении и отпускает при отключении.

        Порты MIDI не присылают события «воткнули/выдернули»: их видно
        только повторным опросом. Вызывается по таймеру, пока звук запущен.
        """
        if not self._midi_enabled or not self._running:
            return
        ports = list_midi_ports()
        if self.midi is not None:
            current = self.midi.port_name
            wanted = pick_port(self._midi_port, ports) if self._midi_port else (
                ports[0] if ports else "")
            if current and wanted and port_key(wanted) == port_key(current):
                return
            self.log(f"MIDI-клавиатура «{current}» отключена.")
            self.midi.close()
            self.midi = None
            self.synth = None
        if self._midi_port and not pick_port(self._midi_port, ports):
            if self._midi_absent != self._midi_port:
                self._midi_absent = self._midi_port
                self.log(f"MIDI «{self._midi_port}» не подключена, жду.")
            return
        # Не чаще раза в 5 секунд: неудачная попытка иначе писала бы
        # в журнал одну и ту же строку каждые два такта таймера.
        now = time.monotonic()
        if now - self._midi_last_try < 5.0:
            return
        self._midi_last_try = now
        self._open_midi()

    def set_metronome(self, enabled: bool, bpm: int = 90, beats: int = 4) -> None:
        self.metronome.configure(enabled, bpm, beats)

    @property
    def running(self) -> bool:
        """Открыт ли звуковой поток прямо сейчас."""
        return self._running

    # -- остановка ----------------------------------------------------------

    def stop(self) -> None:
        self._running = False
        if self._stat_connected:
            span = time.monotonic() - self._stat_t0
            self._stat_connected = False
            if span >= 5.0:
                try:
                    self.log(self._stats_text(span, final=True))
                except Exception:
                    pass
        self._reset_stat_counters()
        if self._out is not None:
            try:
                self._out.stop()
                self._out.close()
            except Exception:
                pass
            self._out = None
        self._out_index = -1
        self._out_measured = None
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
        # Дожидаемся потоков, прежде чем стартовать новые: иначе старый
        # отправитель переживёт перезапуск, будет тянуть пакеты из общей
        # очереди и слать их в закрытый сокет.
        for thread in (self._send_thread, self._udp_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.5)
        self._send_thread = None
        self._udp_thread = None
        self._peer = None
        self.student.clear()
        self.student_r.clear()
        self._recv_stereo = False
        self._peer_stereo = False
        self._peer_opus = False
        self._peer_caps = False
        self._peer_live = False
        self._peer_wait = 0.0
        self._peer_gap = False
        self._peer_last = [0.0, 0.0]
        self._peer_fade = [0, 0]
        while True:
            try:
                self._send_q.get_nowait()
            except queue.Empty:
                break

    # -- громкость ----------------------------------------------------------

    def set_volume(self, key: str, value_0_100: int,
                   peer: bool = False) -> None:
        """peer=True — громкость на стороне собеседника, иначе своя."""
        level = max(0, min(100, int(value_0_100))) / 100.0
        target = self.volumes_peer if peer else self.volumes
        target[key] = level
        if peer:
            source = self.sources.get(key)
            if source is not None:
                source.volume = level

    def set_mic_muted(self, muted: bool) -> None:
        """Микрофон выключен: входы не попадают в поток собеседнику."""
        self.mic_muted = bool(muted)

    def set_buffer_preset(self, name: str) -> str:
        """Набор буферов: low, normal или stable."""
        self.buffer_preset = set_buffer_preset(name)
        # Размер буфера меняет автоматическую компенсацию задержки.
        self._apply_tx_delay()
        return self.buffer_preset

    def set_tx_delay(self, ms: int) -> int:
        """Компенсация задержки передаваемого клика, MIDI и трека.

        0 — автоматически: программа берёт задержку входа и буфера.
        Другое значение — ручная подстройка в миллисекундах.
        """
        self.tx_delay_ms = max(0, min(1000, int(ms)))
        return self._apply_tx_delay()

    def set_opus_bitrate(self, kbps: int) -> int:
        """Битрейт сжатия Opus в кбит/с: 96, 128, 192 или 320.

        Действует на исходящий поток. Новое значение подхватит следующий
        кодировщик: тот, что уже работает, доживает до конца соединения.
        """
        value = int(kbps or 0)
        if value not in (96, 128, 192, 320):
            value = 96
        self.opus_bitrate = value
        return value

    def _out_latency(self) -> float:
        """Задержка вывода в секундах: на столько позже выходит звук клика.

        Музыкант слышит клик через наушники не в момент его рождения в
        микшере, а на задержку вывода позже — и играет ровно на столько
        же позже. Значит, и клик в поток собеседнику надо задержать на неё
        тоже: иначе у собеседника клик опережает голос.
        """
        value = _stream_latency(self._out) if self._out is not None else None
        values = [item for item in (value, self._out_measured)
                  if item is not None and item > 0]
        if values:
            return max(values)
        return _hostapi_latency(self._out_index) or 0.0

    def _in_latency(self) -> float:
        """Задержка самого медленного входа в секундах."""
        in_lat = 0.0
        # Список входов снимаем копией: его меняет интерфейс, а считаем мы
        # в звуковом потоке.
        for source in list(self.sources.values()):
            value = source.latency_now()
            if value > in_lat:
                in_lat = value
        return in_lat

    def _measure_tx_delay(self) -> int:
        """Автоматическая задержка тракта в миллисекундах.

        Голос появляется в отправляемом потоке позже клика ровно на столько:
        задержка вывода (пока музыкант услышит клик и сыграет) плюс задержка
        входа и заполнение входного буфера. Выход обязателен: без него клик
        у собеседника опережает голос на всю задержку наушников, а на
        медленном MME-драйвере это сто миллисекунд и больше.
        Заполнение берём фактическое, если оно уже измерено; до первых
        звуков — середину между границами гистерезиса.
        """
        if not self.sources:
            # Входов нет — выравнивать не с чем.
            return 0
        if self._tx_fill > 0:
            ring_frames = self._tx_fill
        else:
            keep, maximum = input_bounds()
            ring_frames = 0.5 * (keep + maximum)
        out_lat = self._out_latency()
        in_lat = self._in_latency()
        total = (out_lat * 1000.0 + in_lat * 1000.0
                 + ring_frames * 1000.0 / SAMPLE_RATE)
        self._tx_parts = (out_lat * 1000.0, in_lat * 1000.0,
                          ring_frames * 1000.0 / SAMPLE_RATE,
                          int(round(total)))
        return int(round(total))

    def _apply_tx_delay(self) -> int:
        """Пересчитывает задержку и перестраивает линию задержки."""
        auto = self._measure_tx_delay()
        self._tx_delay_auto = auto
        value = auto if self.tx_delay_ms <= 0 else self.tx_delay_ms
        self._tx_delay.set_delay(int(round(value * SAMPLE_RATE / 1000.0)))
        return value

    def _tx_target(self, fill: int) -> float:
        """Куда подтягивать задержку передачи — по фактическому буферу.

        Буфер живёт по своему расписанию: его подрезают на пороге, он снова
        наполняется, а медленный драйвер ещё и отдаёт звук пачками. Поэтому
        расчётная середина буфера не совпадает с тем, на сколько голос
        отстаёт на самом деле, и клик у собеседника уезжает на десятки
        миллисекунд. Берём то, что в буфере лежит, и сглаживаем: важен
        средний уровень, а не колебание на размер блока.
        """
        out_lat = self._out_latency()
        # Ту же задержку входа, что и при старте, но уже по отметкам
        # драйвера, если они успели набраться.
        in_lat = self._in_latency()
        if self.tx_delay_ms > 0:
            self._tx_parts = (out_lat * 1000.0, in_lat * 1000.0,
                              fill * 1000.0 / SAMPLE_RATE, self.tx_delay_ms)
            return self.tx_delay_ms * SAMPLE_RATE / 1000.0
        if fill <= 0:
            return float(self._tx_delay.frames)
        snap = False
        if self._tx_fill <= 0.0:
            self._tx_fill = float(fill)
        else:
            delta = float(fill) - self._tx_fill
            if delta <= -0.015 * SAMPLE_RATE:
                # Входной буфер подрезали или драйвер выбрал пачку — голос
                # разом прыгнул вперёд на полтора десятка миллисекунд и
                # больше. Плавно тянуть за ним клик полминуты нельзя: все
                # эти секунды клик идёт мимо хлопков. Переставляем линию
                # задержки сразу, тем же блоком, что и голос.
                self._tx_fill = float(fill)
                snap = True
            else:
                self._tx_fill += delta * 0.05
        total = self._tx_fill + (in_lat + out_lat) * SAMPLE_RATE
        if snap:
            self._tx_delay.set_delay(int(round(total)))
        self._tx_parts = (out_lat * 1000.0, in_lat * 1000.0,
                          self._tx_fill * 1000.0 / SAMPLE_RATE,
                          int(round(total * 1000.0 / SAMPLE_RATE)))
        return total

    def _log_tx_parts(self, frames: int) -> None:
        """Раз в минуту пишет в журнал, из чего сложилась компенсация.

        По числам видно, что именно разъехалось: медленный драйвер вывода,
        вход или переполненный буфер. Без этого разбирать рассинхрон
        приходится только на слух.
        """
        self._tx_log_t += frames / SAMPLE_RATE
        if self._tx_log_t < 60.0:
            return
        self._tx_log_t = 0.0
        out_ms, in_ms, fill_ms, total_ms = self._tx_parts
        how = "вручную" if self.tx_delay_ms > 0 else "авто"
        if not self.sources:
            return
        # Отметок вывода нет — цифра выхода оценочная. По этой пометке в
        # журнале видно, почему компенсация может не сойтись.
        note = "" if self._out_measured is not None else ", выход оценочный"
        try:
            self.log(f"Компенсация ({how}): выход {out_ms:.0f} мс, "
                     f"вход {in_ms:.0f} мс, буфер {fill_ms:.0f} мс, "
                     f"итого {total_ms} мс{note}.")
        except Exception:
            pass

    # -- микшер -------------------------------------------------------------

    def _out_callback(self, outdata, frames, time_info, status) -> None:
        """Обёртка: сбой в микшере не должен убивать звуковой поток.

        Если исключение выйдет из callback, PortAudio останавливает поток,
        и звук пропадает у обеих сторон молча. Поэтому ловим всё, отдаём
        тишину и считаем сбои — они видны в журнале.
        """
        try:
            # Драйвер сам говорит, что не успел забрать блок. Это слышно
            # как треск, и раньше никто этого не замечал — теперь считаем.
            if status and getattr(status, "output_underflow", False):
                self._out_underflow += 1
        except Exception:
            pass
        try:
            # Задержку вывода меряем по отметкам драйвера: от вызова callback
            # до момента, когда блок зазвучит. Паспортная у WASAPI бывает
            # нулём, и клик у собеседника тогда опережает голос на всю
            # задержку наушников.
            value = _stamp_latency(time_info, "out")
            if value is not None:
                if self._out_measured is None:
                    self._out_measured = value
                else:
                    self._out_measured += (value - self._out_measured) * 0.1
            self._out_callback_inner(outdata, frames)
        except Exception as exc:
            self._cb_errors += 1
            outdata.fill(0)
            if self._cb_errors == 1 or self._cb_errors % 200 == 0:
                try:
                    self.log(f"Сбой в звуковом потоке ({self._cb_errors}): "
                             f"{exc.__class__.__name__}: {exc}")
                except Exception:
                    pass

    def _out_callback_inner(self, outdata, frames) -> None:
        self._last_cb = time.monotonic()
        self._out_frames += frames
        # Синтезатор рендерится ровно один раз: у него внутри фаза и огибающие,
        # второй вызов съел бы часть звука.
        synth = self.synth
        synth_out = None
        if synth is not None:
            try:
                synth_out = synth.render(frames)
            except Exception:
                synth_out = None
        # Минус и метроном всегда играет только та сторона, у которой они
        # включены. Звук рождается здесь и уходит в поток — у собеседника
        # свой метроном и плеер молчат, он слышит только пришедшее. Поэтому
        # переключателя «чья сторона» нет: включил у себя — играет у обоих.
        midi_local = self.volumes.get("midi", 0.8)
        midi_peer = self.volumes_peer.get("midi", 0.8)
        metro_local = self.volumes.get("metronome", 0.6)
        metro_peer = self.volumes_peer.get("metronome", 0.6)
        clicks = self.metronome.render(frames)

        # Трек рендерится ровно один раз (внутри позиция) и идёт в обе
        # стороны: в наушники и в поток собеседнику. Поэтому у ученика нет
        # своего плеера, который надо синхронизировать — он слышит трек в
        # общем потоке с той же задержкой, что и голос.
        track = self.player.render(frames)
        track_local = track_peer = None
        if track is not None:
            if self.volumes.get("player", 0.8) > 0:
                track_local = track * self.volumes["player"]
            if self.volumes_peer.get("player", 0.8) > 0:
                track_peer = track * self.volumes_peer["player"]

        # В наушники: собеседник, своя MIDI-клавиатура и свой метроном.
        # Источники со входов сюда не идут — иначе микрофон поймает колонки
        # и получится эхо.
        # Запас на медленном выводе нужен и приёмному кольцу: тот же драйвер
        # зовёт обратный вызов пачками, и пачка вычерпывает не только
        # микрофонный буфер, но и звук собеседника. Раньше запас давали
        # только входу, поэтому у медленного драйвера рвался именно приём.
        keep, maximum = student_bounds()
        target = keep + (maximum - keep) // 4
        student_vol = self.volumes["student"]
        # Первое наполнение: пока буфер не набрал цель, лучше молчать
        # несколько десятков миллисекунд, чем начать связь с треска.
        # Предохранитель на 1,5 с — на случай совсем плохой сети, чтобы
        # не остаться без звука совсем.
        if not self._peer_live:
            self._peer_wait += frames / SAMPLE_RATE
            if self.student.level >= target or self._peer_wait >= 1.5:
                self._peer_live = True
            else:
                self.student.trim(keep, maximum)
                if self._recv_stereo:
                    self.student_r.trim(keep, maximum)
        if self._peer_live:
            local_l = self._read_peer(self.student, frames, 0) * student_vol
            if self._recv_stereo:
                local_r = self._read_peer(self.student_r, frames, 1) * student_vol
            else:
                # Собеседник шлёт моно: правый канал — копия левого.
                local_r = local_l.copy()
        else:
            local_l = np.zeros(frames, dtype=np.float32)
            local_r = local_l.copy()
        if synth_out is not None:
            local_l += synth_out * midi_local
            local_r += synth_out * midi_local
        local_l += clicks * metro_local
        local_r += clicks * metro_local
        if track_local is not None:
            local_l[:track_local.size] += track_local
            local_r[:track_local.size] += track_local
        # Мягкий лимитер, а не жёсткий срез: громкий аккорд или хор
        # поджимается плавно и не превращается в цифровой хрип.
        local_l = soft_limit(local_l)
        local_r = soft_limit(local_r)
        np.clip(local_l, -1.0, 1.0, out=local_l)
        np.clip(local_r, -1.0, 1.0, out=local_r)
        if self._out_channels > 1:
            outdata[:, 0] = local_l
            outdata[:, 1] = local_r
        else:
            # Моно-выход: сводим каналы, чтобы никто не потерялся.
            outdata[:, 0] = 0.5 * (local_l + local_r)

        # Собеседнику: входы, MIDI и метроном — всё, что он должен слышать.
        # Входы читаем всегда, даже при выключенном микрофоне: иначе буфер
        # копил бы звук и после включения выплюнул старую задержку.
        #
        # Клик, MIDI и трек рождаются здесь же в микшере и ушли бы к
        # собеседнику сразу, а голос идёт через микрофон и входной буфер и
        # приходит позже — на его стороне клик опережал бы хлопок. Поэтому
        # задерживаем только эти сигналы, до голоса; входы идут как есть.
        gen = clicks * metro_peer
        if synth_out is not None:
            gen += synth_out * midi_peer
        if track_peer is not None:
            gen[:track_peer.size] += track_peer
        # Заполнение входных буферов смотрим до чтения: это ровно та
        # задержка, на которую голос отстаёт от клика. По ней и подтягиваем
        # линию задержки — иначе расчётная цифра расходится с буфером на
        # десятки миллисекунд, и метроном у собеседника не совпадает.
        fill = 0
        for source in self.sources.values():
            fill = max(fill, source.fill())
        self._tx_delay.set_target(int(round(self._tx_target(fill))))
        self._log_tx_parts(frames)
        mix_l = self._tx_delay.process(gen)
        mix_r = mix_l.copy()
        stereo_out = False
        for source in self.sources.values():
            left, right = source.read(frames)
            if source.stereo:
                stereo_out = True
            if self.mic_muted:
                continue
            if source.volume > 0:
                mix_l += left * source.volume
                mix_r += right * source.volume

        # Перегруз считаем до лимитера: именно эти блоки раньше срезались
        # жёстко и хрипели. Теперь их поджимает soft_limit, но число
        # по-прежнему подсказывает, что где-то слишком громко.
        try:
            if (float(np.max(np.abs(mix_l))) >= 1.0
                    or float(np.max(np.abs(mix_r))) >= 1.0):
                self._tx_clip += 1
        except Exception:
            pass
        mix_l = soft_limit(mix_l)
        mix_r = soft_limit(mix_r)

        # Стерео шлём, только если собеседник подтвердил, что понимает его.
        # Старая версия получила бы пакет с незнакомой меткой и оглохла.
        if stereo_out and self._peer_stereo:
            interleaved = np.empty(frames * 2, dtype=np.float32)
            interleaved[0::2] = np.clip(mix_l, -1.0, 1.0)
            interleaved[1::2] = np.clip(mix_r, -1.0, 1.0)
            pcm = (interleaved * 32767.0).astype(np.int16).tobytes()
            packet_data = (True, pcm)
        else:
            # Все входы моно (или их нет) — шлём прежним экономным форматом.
            mix = 0.5 * (mix_l + mix_r)
            pcm = (np.clip(mix, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            packet_data = (False, pcm)
        try:
            self._send_q.put_nowait(packet_data)
        except queue.Full:
            self._tx_dropped += 1
        # Запас приёма подстраивается по факту: провал буфера — добавляем,
        # десять секунд чисто — потихоньку отпускаем.
        self._adapt_recv(frames)

    # -- статистика ---------------------------------------------------------

    @staticmethod
    def _caps_flags() -> bytes:
        """Что мы умеем: стерео и, если библиотека нашлась, Opus."""
        flags = CAPS_STEREO
        if opus_codec.available():
            flags |= CAPS_OPUS
        return bytes([flags])

    def _seq_gap(self, data: bytes) -> int:
        """Сколько пакетов потеряно перед этим. Ничего не меняет."""
        last = self._rx_last_seq
        if last is None or len(data) < 8:
            return 0
        seq = struct.unpack_from("!I", data, 4)[0]
        diff = (seq - last) & 0xFFFFFFFF
        return diff - 1 if 1 < diff < 1000 else 0

    def _read_peer(self, ring: Ring, frames: int, channel: int) -> np.ndarray:
        """Читает звук собеседника, подстраивая скорость под уровень буфера.

        Часы двух компьютеров идут чуть по-разному, и буфер либо пустеет,
        либо копит. Копить нельзя — растёт задержка; пустеть тоже — это
        треск. Поэтому читаем то чуть быстрее, то чуть медленнее цели,
        растягивая блок на доли процента: на слух незаметно, а буфер
        держится около цели. Подрезку до max оставляем как аварийный
        выход, но после неё плавно входим — без щелчка.
        """
        keep, maximum = student_bounds()
        target = keep + (maximum - keep) // 4
        ring.trim(target, maximum)
        fade = self._peer_fade[channel]
        if ring.cut_flag:
            # Выбросили кусок из середины — на стыке был бы щелчок.
            fade = FADE_FRAMES
        level = ring.level
        ratio = 1.0
        # Мёртвая зона: пока буфер рядом с целью, скорость не трогаем.
        # Прежняя подстройка реагировала уже на пару миллисекунд и тянула
        # весь входящий поток на процент — хлопки собеседника «плавали» по
        # скорости, хотя щелчки у него шли ровно. Часы двух компьютеров
        # расходятся на сотые доли процента, и подстройки 0.15% хватает с
        # запасом, а на слух она неразличима.
        dead = max(AUDIO_BLOCK, int(target * 0.2))
        if level > target + dead:
            over = level - target - dead
            ratio = 1.0 + min(0.0015, over / max(1.0, target * 10.0))
        elif level < target - dead:
            under = target - dead - level
            ratio = 1.0 - min(0.0015, under / max(1.0, target * 10.0))
        n = frames if ratio == 1.0 else max(1, int(round(frames * ratio)))
        out = ring.read(n)
        got = ring.last_got
        starved = got < n
        if starved:
            # Не хватило звука: гасим с последнего отсчёта, а не обрывом.
            start = float(self._peer_last[channel])
            if got:
                start = float(out[got - 1])
            span = min(n - got, FADE_FRAMES)
            if span > 0:
                out[got:got + span] = start * np.linspace(
                    1.0, 0.0, span, dtype=np.float32)
            self._recv_starve += n - got
            fade = FADE_FRAMES
        if n != frames:
            # Растягиваем или сжимаем блок до нужной длины. Линейная
            # интерполяция стоит дёшево и на десятимиллисекундном блоке
            # не слышна.
            src = np.arange(n, dtype=np.float32)
            dst = np.linspace(0.0, float(n - 1), frames, dtype=np.float32)
            out = np.interp(dst, src, out).astype(np.float32)
        if fade > 0 and not starved:
            span = min(frames, fade)
            out[:span] *= np.linspace(0.0, 1.0, span, dtype=np.float32)
            fade -= span
        self._peer_fade[channel] = fade
        self._peer_last[channel] = float(out[-1]) if out.size else 0.0
        return out

    def _adapt_recv(self, frames: int) -> None:
        """Поднимает запас приёма по факту провала и медленно отпускает.

        Постоянный запас из start() учитывает медленный драйвер, но не
        сеть. Если приём всё равно голодал, добавляем буфер: треск хуже
        лишних ста миллисекунд задержки. Через десять секунд чистого
        приёма запас уменьшается — по шагу, чтобы не устроить новый провал.
        """
        if self._recv_starve > 0:
            deficit = self._recv_starve
            self._recv_starve = 0
            add = min(BLOCK * 8, max(BLOCK, deficit * 2))
            self._recv_adapt = min(BLOCK * 40, self._recv_adapt + add)
            self._apply_recv_reserve(True)
            self._recv_clean_t = 0.0
            return
        self._recv_clean_t += frames / SAMPLE_RATE
        if self._recv_clean_t >= 10.0:
            self._recv_clean_t = 0.0
            if self._recv_adapt > 0:
                self._recv_adapt = max(0, self._recv_adapt - BLOCK * 2)
                self._apply_recv_reserve(False)

    def _apply_recv_reserve(self, grow: bool) -> None:
        total = self._recv_base + self._recv_adapt
        set_recv_reserve(total)
        ms = total * 1000 // SAMPLE_RATE
        if grow:
            # Запас растёт шагами по 10–80 мс: во время рваной связи такие
            # шаги идут подряд, а журнал не должен захлебнуться. Пишем не
            # чаще раза в две секунды.
            now = time.monotonic()
            if now - self._recv_note_t >= 2.0:
                self._recv_note_t = now
                self._recv_note = (f"Звук собеседника рвался — запас "
                                   f"приёма увеличен до {ms} мс.")
        else:
            self._recv_note = (f"Приём устойчив — запас приёма уменьшен "
                               f"до {ms} мс.")

    def _write_peer(self, samples: np.ndarray, stereo: bool) -> None:
        """Кладёт пришедший звук в буферы воспроизведения."""
        if samples.size == 0:
            return
        data = samples.astype(np.float32) / 32768.0
        if self._peer_gap:
            # После потери пакетов и предсказания первый настоящий звук
            # начинается не с нуля — без плавного входа слышен щелчок.
            self._peer_gap = False
            if stereo:
                pair = data.size // 2
                span = min(FADE_FRAMES, pair)
                if span > 0:
                    ramp = np.linspace(0.0, 1.0, span, dtype=np.float32)
                    data[0:span * 2:2] *= ramp
                    data[1:span * 2:2] *= ramp
            else:
                span = min(FADE_FRAMES, data.size)
                if span > 0:
                    data[:span] *= np.linspace(0.0, 1.0, span,
                                               dtype=np.float32)
        if stereo:
            frames = data.size // 2
            if not frames:
                return
            data = data[:frames * 2]
            self._recv_stereo = True
            self.student.write(data[0::2].copy())
            self.student_r.write(data[1::2].copy())
        else:
            self._recv_stereo = False
            self.student.write(data)

    def _note_seq(self, data: bytes) -> None:
        """Считает потери по номеру пакета.

        UDP не пересылает потерянное, и дырка в нумерации — это ровно тот
        звук, которого не хватило в буфере. Без этого числа нельзя отличить
        хрип от перегруза входа от хрипа из-за сети.
        """
        self._rx_packets += 1
        if len(data) < 8:
            return
        seq = struct.unpack_from("!I", data, 4)[0]
        last = self._rx_last_seq
        self._rx_last_seq = seq
        if last is None:
            return
        diff = (seq - last) & 0xFFFFFFFF
        if diff == 1:
            return
        if diff == 0:
            self._rx_ooo += 1
        elif diff < 1000:
            self._rx_lost += diff - 1
        else:
            # Пакет из прошлого или собеседник перезапустил программу:
            # это не потеря, считаем отдельно.
            self._rx_ooo += 1

    def _reset_stat_counters(self) -> None:
        self._stat_t0 = 0.0
        self._rx_packets = 0
        self._rx_lost = 0
        self._rx_ooo = 0
        self._rx_last_seq = None
        self._tx_packets = 0
        self._tx_dropped = 0
        self._tx_nopeer = 0
        self._tx_clip = 0
        self._stat_frames_t0 = self._out_frames
        self.student.reset_stats()
        self.student_r.reset_stats()
        # Буферы входов тоже обнуляем: иначе «пустых буферов» копится с
        # запуска программы и к окну статистики не относится.
        for source in self.sources.values():
            source.ring.reset_stats()
            if source.ring_r is not None:
                source.ring_r.reset_stats()
            self._source_frames[source.key] = source.frames_in

    def _stats_text(self, seconds: float, final: bool = False) -> str:
        rings = [self.student, self.student_r]
        in_rings: list = []
        peak = 0.0
        in_clip = 0
        for source in self.sources.values():
            rings.append(source.ring)
            in_rings.append(source.ring)
            if source.ring_r is not None:
                rings.append(source.ring_r)
                in_rings.append(source.ring_r)
            if source.peak > peak:
                peak = source.peak
            in_clip += source.clipped
            source.peak = 0.0
            source.clipped = 0
        gaps_ms = sum(r.underruns for r in rings) * 1000.0 / SAMPLE_RATE
        # Отдельно считаем голод приёмного буфера (звук собеседника) и
        # входных (свой микрофон). Это разные поломки: первая значит, что
        # до нас не дошёл звук, вторая — что мы отправили тишину вместо
        # голоса, и трещит тогда у собеседника.
        peer_gap_ms = (self.student.underruns + self.student_r.underruns) * 1000.0 / SAMPLE_RATE
        mic_gap_ms = sum(r.underruns for r in in_rings) * 1000.0 / SAMPLE_RATE
        over_ms = sum(r.overflows for r in rings) * 1000.0 / SAMPLE_RATE
        # Считаем за отрезок между записями в журнал, а не с запуска.
        for ring in rings:
            ring.reset_stats()
        # Микрофон, открытый и молчащий, звук не портит, но создаёт
        # впечатление, что он работает. Лучше честно написать в журнал.
        for source in self.sources.values():
            now_frames = source.frames_in
            before = self._source_frames.get(source.key, now_frames)
            self._source_frames[source.key] = now_frames
            if now_frames - before <= 0:
                self.log(f"Вход «{device_name(source.device_index)}» "
                         f"не присылает звук.")
        # Сколько звука реально выведено за этот отрезок. Ниже 90 % — звук
        # шёл рывками, и это видно раньше, чем человек пожалуется на хрипы.
        frames = self._out_frames - self._stat_frames_t0
        self._stat_frames_t0 = self._out_frames
        realtime = frames / max(1e-6, seconds * SAMPLE_RATE) * 100.0
        head = "Итог звука" if final else "Звук"
        # Запас приёма и отказы драйвера: по ним видно, что связь рвётся не
        # из-за сети, а из-за звуковой карты или слишком маленького буфера.
        recv_ms = (self._recv_base + self._recv_adapt) * 1000 // SAMPLE_RATE
        out_under = self._out_underflow
        self._out_underflow = 0
        status_ovf = 0
        for source in self.sources.values():
            status_ovf += source.status_overflows
            source.status_overflows = 0
        parts = [
            f"принято {self._rx_packets} пакетов",
            f"потеряно {self._rx_lost}",
            f"не по порядку {self._rx_ooo}",
            f"пустых буферов {gaps_ms:.0f} мс "
            f"(собеседник {peer_gap_ms:.0f}, микрофон {mic_gap_ms:.0f})",
            f"выброшено {over_ms:.0f} мс",
            f"запас приёма {recv_ms} мс",
            f"уровень входа {peak:.2f}",
            f"перегруз входа {in_clip}",
            f"отправлено {self._tx_packets}",
            f"не ушло {self._tx_nopeer + self._tx_dropped}",
            f"ошибок отправки {self._tx_errors}",
            f"перегруз отправки {self._tx_clip}",
            f"выведено {realtime:.0f}% реального времени",
            f"отказов вывода {out_under}",
            f"переполнений драйвера {status_ovf}",
            f"сбоев потока {self._cb_errors}",
        ]
        return f"{head} за {seconds:.0f} с: " + ", ".join(parts) + "."

    def _maybe_log_stats(self, now: float) -> None:
        # Сообщение об изменении запаса приёма пишем один раз, даже если
        # собеседник уже отключился: это итог, а не текущее состояние.
        note = self._recv_note
        if note:
            self._recv_note = ""
            try:
                self.log(note)
            except Exception:
                pass
        if self._peer is None:
            return
        if not self._stat_connected:
            # Связь только что установилась: счёт с этого момента, иначе
            # в статистику попал бы звук, ушедший в пустоту до подключения.
            self._reset_stat_counters()
            self._stat_connected = True
            self._stat_t0 = now
            return
        if now - self._stat_t0 < 60.0:
            return
        span = now - self._stat_t0
        self._stat_t0 = now
        try:
            self.log(self._stats_text(span))
        except Exception:
            pass

    def _watchdog_loop(self) -> None:
        """Сторож вывода: если обратные вызовы встали, выход переоткрывается.

        Живёт отдельно от звукового потока: упавший или замолчавший callback
        сам о себе не сообщит. Раз в две секунды сверяем время последнего
        вызова; молчание дольше двух секунд — переоткрываем выход.
        """
        while True:
            time.sleep(2.0)
            if not self._running or self._out is None:
                continue
            now = time.monotonic()
            if now - self._last_cb <= 2.0:
                continue
            if now - self._last_reopen < 10.0:
                continue
            self._last_reopen = now
            try:
                self.log("Звук встал: обратных вызовов нет дольше 2 секунд. "
                         "Переоткрываю выход.")
            except Exception:
                pass
            args = self._start_args
            if not args:
                continue
            try:
                ok, message = self.start(**args)
                if not ok:
                    self.log(f"Переоткрыть выход не удалось: {message}")
            except Exception as exc:
                self.log(f"Переоткрыть выход не удалось: {exc}")

    # -- сеть ---------------------------------------------------------------

    def _udp_loop(self, generation: int) -> None:
        sock = self._sock
        # Декодеры по числу каналов; None — этот компьютер не смог.
        decoders: dict[int, object] = {}
        while (self._running and sock is not None
               and generation == self._generation):
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

            if magic == MAGIC_CAPS:
                # Собеседник сообщил, что умеет: бит 0 — стерео, бит 1 —
                # Opus. Старая версия присылает один байт b"\x01", поэтому
                # незнакомый хвост просто не учитываем.
                self._peer = addr if self._is_server else self._server_addr
                flags = data[4] if len(data) > 4 else CAPS_STEREO
                self._peer_stereo = bool(flags & CAPS_STEREO)
                self._peer_opus = bool(flags & CAPS_OPUS)
                self._peer_caps = True
                if self._is_server:
                    try:
                        sock.sendto(MAGIC_CAPS + self._caps_flags(), addr)
                    except OSError:
                        pass
                continue

            # Любой пакет от собеседника подтверждает его адрес.
            if self._peer is None and magic in (MAGIC_AUDIO,
                                                MAGIC_AUDIO_STEREO,
                                                MAGIC_AUDIO_OPUS,
                                                MAGIC_AUDIO_OPUS_STEREO,
                                                MAGIC_VIDEO):
                self._peer = addr

            if magic == MAGIC_AUDIO:
                if self._seq_gap(data):
                    self._peer_gap = True
                self._note_seq(data)
                pcm = np.frombuffer(data[8:], dtype=np.int16)
                self._recv_stereo = False
                self.student.write(pcm.astype(np.float32) / 32768.0)
            elif magic == MAGIC_AUDIO_STEREO:
                if self._seq_gap(data):
                    self._peer_gap = True
                self._note_seq(data)
                # Пары int16 вперемешку: левый, правый, левый, правый…
                samples = np.frombuffer(data[8:], dtype=np.int16)
                frames = samples.size // 2
                if frames:
                    samples = samples[:frames * 2].astype(np.float32) / 32768.0
                    self._recv_stereo = True
                    self.student.write(samples[0::2].copy())
                    self.student_r.write(samples[1::2].copy())
            elif magic in (MAGIC_AUDIO_OPUS, MAGIC_AUDIO_OPUS_STEREO):
                stereo = magic == MAGIC_AUDIO_OPUS_STEREO
                gap = self._seq_gap(data)
                self._note_seq(data)
                if gap:
                    self._peer_gap = True
                channels = 2 if stereo else 1
                if channels not in decoders:
                    try:
                        decoders[channels] = opus_codec.Decoder(channels)
                    except Exception as exc:
                        decoders[channels] = None
                        self._opus_errors += 1
                        try:
                            self.log(f"Приём Opus не завёлся, звук пойдёт "
                                     f"несжатым: {exc}")
                        except Exception:
                            pass
                decoder = decoders[channels]
                if decoder is None:
                    continue
                try:
                    if gap == 1:
                        # В пакете лежит ослабленная копия предыдущего кадра:
                        # восстановить её точнее, чем предсказывать.
                        fec = decoder.decode(data[8:], fec=True)
                        self._write_peer(np.frombuffer(fec, np.int16),
                                         stereo)
                    elif gap > 1:
                        # Дырку закрываем предсказанием. Раньше хватало
                        # 50 мс, но и после них в буфере оставалась дыра,
                        # которая слышалась треском: теперь до 200 мс, а
                        # настоящий звук после дырки входит плавно.
                        for _ in range(min(gap, 20)):
                            plc = decoder.decode(b"")
                            self._write_peer(np.frombuffer(plc, np.int16),
                                             stereo)
                    pcm = decoder.decode(data[8:])
                except Exception as exc:
                    self._opus_errors += 1
                    if self._opus_errors == 1:
                        try:
                            self.log(f"Сбой декодера Opus: {exc}")
                        except Exception:
                            pass
                    continue
                self._write_peer(np.frombuffer(pcm, np.int16), stereo)
            elif magic == MAGIC_VIDEO and self.on_video is not None:
                self.on_video(data)

    def _send_loop(self, generation: int) -> None:
        sock = self._sock
        send_q = self._send_q
        hello_at = 0.0
        caps_at = 0.0
        caps_sent = 0
        # Когда собеседник появился и когда мы уже сказали в журнал, каким
        # форматом идёт звук. Без этой строки по журналу не понять, почему
        # один компьютер сжимает звук, а второй шлёт его целиком.
        peer_at = 0.0
        mode_logged = False
        # Своя библиотека Opus есть? Если нет — о сжатии не заикаемся.
        local_opus = opus_codec.available()
        encoders: dict[int, object] = {}
        # Звуковая карта отдаёт блоки по 5 мс, а Opus работает кадром 10 мс:
        # копим неполный кадр здесь.
        pending = bytearray()
        pending_stereo = False
        opus_logged = False
        while (self._running and sock is not None
               and generation == self._generation):
            now = time.monotonic()
            self._maybe_log_stats(now)
            if not self._is_server and self._peer is None and self._server_addr:
                if now - hello_at > 1.0:
                    hello_at = now
                    try:
                        sock.sendto(MAGIC_HELLO, self._server_addr)
                    except OSError:
                        pass
            # Как только связь есть, сообщаем, что умеем: стерео и, если
            # библиотека нашлась, Opus. Отправляем свои возможности всегда,
            # пока собеседник не подтвердил свои: раньше условие замолкало,
            # как только приходили его возможности, и он мог так и не
            # узнать про наше сжатие. Пять повторов на случай потери пакета,
            # и до двенадцати, если подтверждения всё нет.
            if self._peer is not None and now - caps_at > 1.0:
                if peer_at == 0.0:
                    peer_at = now
                limit = 5 if self._peer_caps else 12
                if caps_sent < limit:
                    caps_at = now
                    caps_sent += 1
                    try:
                        sock.sendto(
                            MAGIC_CAPS + self._caps_flags(), self._peer)
                    except OSError:
                        pass
            # Через несколько секунд после установления связи пишем в
            # журнал, каким форматом реально идёт звук.
            if (self._peer is not None and not mode_logged and peer_at > 0.0
                    and now - peer_at >= 3.0
                    and (self._peer_caps or now - peer_at >= 6.0)):
                mode_logged = True
                try:
                    if self._peer_opus and local_opus:
                        bits = self.opus_bitrate
                        mode = "стерео" if self._peer_stereo else "моно"
                        self.log(f"Формат звука: Opus, {bits} кбит/с, {mode}.")
                    elif not local_opus:
                        self.log("Формат звука: несжатый PCM, "
                                 "библиотека Opus не найдена.")
                    elif not self._peer_opus:
                        self.log("Формат звука: несжатый PCM, собеседник "
                                 "не подтвердил сжатие (старая версия?).")
                except Exception:
                    pass
            try:
                stereo, pcm = send_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._peer is None:
                self._tx_nopeer += 1
                continue
            if not (self._peer_opus and local_opus):
                # Собеседник сжатый звук не понимает — шлём как раньше.
                pending.clear()
                self._send_audio(sock, stereo, pcm, opus=False)
                continue
            if pending and stereo != pending_stereo:
                pending.clear()
            pending_stereo = stereo
            pending.extend(pcm)
            frame_bytes = BLOCK * (4 if stereo else 2)
            while len(pending) >= frame_bytes:
                chunk = bytes(pending[:frame_bytes])
                del pending[:frame_bytes]
                encoder = self._encoder(encoders, 2 if stereo else 1)
                payload = b""
                if encoder is not None:
                    try:
                        payload = encoder.encode(chunk)
                    except Exception as exc:
                        self._opus_errors += 1
                        self._peer_opus = False
                        if self._opus_errors == 1:
                            try:
                                self.log(f"Кодер Opus отказал, звук пойдёт "
                                         f"несжатым: {exc}")
                            except Exception:
                                pass
                if not payload:
                    self._send_audio(sock, stereo, chunk, opus=False)
                    continue
                self._send_audio(sock, stereo, payload, opus=True)
                if not opus_logged:
                    opus_logged = True
                    try:
                        self.log(f"Звук сжимается кодеком Opus, "
                                 f"{self.opus_bitrate} кбит/с.")
                    except Exception:
                        pass
        for encoder in encoders.values():
            if encoder is not None:
                try:
                    encoder.close()
                except Exception:
                    pass

    def _encoder(self, cache: dict, channels: int):
        """Кодировщик на нужное число каналов; None — этот компьютер не смог."""
        # Ключ включает битрейт: при смене настройки на лету создаётся
        # новый кодировщик, а старый закрывается вместе с потоком отправки.
        key = (channels, self.opus_bitrate)
        if key in cache:
            return cache[key]
        try:
            encoder = opus_codec.Encoder(
                channels, self.opus_bitrate * 1000)
        except Exception as exc:
            cache[key] = None
            if not self._opus_fail_logged:
                self._opus_fail_logged = True
                try:
                    self.log(f"Opus недоступен, звук пойдёт без сжатия: "
                             f"{exc}")
                except Exception:
                    pass
            return None
        cache[key] = encoder
        return encoder

    def _send_audio(self, sock, stereo: bool, payload: bytes,
                    opus: bool) -> None:
        """Собирает и отправляет один звуковой пакет."""
        if self._peer is None:
            self._tx_nopeer += 1
            return
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        if opus:
            magic = (MAGIC_AUDIO_OPUS_STEREO if stereo
                     else MAGIC_AUDIO_OPUS)
        else:
            magic = MAGIC_AUDIO_STEREO if stereo else MAGIC_AUDIO
        packet = magic + struct.pack("!I", self._seq) + payload
        try:
            sock.sendto(packet, self._peer)
            self._tx_packets += 1
        except OSError as exc:
            # Молча проглатывать нельзя: именно так и терялся звук,
            # когда старый поток слал в уже закрытый сокет.
            self._tx_errors += 1
            if self._tx_errors == 1 or self._tx_errors % 500 == 0:
                try:
                    self.log(f"Ошибка отправки звука ({self._tx_errors}): "
                             f"{exc}")
                except Exception:
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
