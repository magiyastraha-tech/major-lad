"""MIDI-вход и простой синтезатор.

Пользователь просил «MIDI-движок от Microsoft», чтобы клавиатура играла
и ученик это слышал. В Windows родной MIDI-движок — это MME/WinMM, и
доступ к нему в Python даёт python-rtmidi. Звук синтезируется здесь же,
полифонической синусоидой с мягкой атакой: так не нужны SoundFont-банки
и лишние DLL, а сборка остаётся портативной в один exe.

Синус — не рояль, но для проверки связи, настройки и показа нот этого
достаточно. Подключить полноценный GM-синтезатор можно позже, не меняя
остальную программу: интерфейс Synth небольшой.
"""
from __future__ import annotations

import math
import re
import threading

import numpy as np

try:
    import rtmidi
except Exception:  # pragma: no cover
    rtmidi = None

def list_midi_ports() -> list[str]:
    """Имена доступных MIDI-входов. Пустой список — устройства не найдены."""
    if rtmidi is None:
        return []
    probe = None
    try:
        probe = rtmidi.MidiIn()
        return [str(name) for name in probe.get_ports()]
    except Exception:
        return []
    finally:
        if probe is not None:
            try:
                del probe
            except Exception:
                pass


def port_key(name: str) -> str:
    """Имя порта для сравнения — без хвостового номера и регистра.

    Windows прибавляет к имени копии число: «Impact LX49+ 1», а после
    переподключения номер может стать другим или исчезнуть. Сравнивать
    точные строки из-за этого нельзя.
    """
    return re.sub(r"\s+\d+$", "", (name or "").strip()).strip().lower()


def pick_port(port_name: str, ports: list[str]) -> str:
    """Какой порт открывать. Пустая строка — подходящего нет."""
    if not ports:
        return ""
    if port_name:
        if port_name in ports:
            return port_name
        key = port_key(port_name)
        for name in ports:
            if port_key(name) == key:
                return name
        # Сохранённого устройства нет среди подключённых.
        return ""
    return ports[0]


MAX_VOICES = 24
# Огибающая считается на каждом отсчёте, а не на каждом блоке: ступеньки
# амплитуды на границе блоков дают призвук, который на слух читается как
# хрипотца. Атака короткая, отпускание мягче — как у язычкового инструмента.
ATTACK_SECONDS = 0.006
RELEASE_SECONDS = 0.045
# Украденный голос не обрываем: за несколько миллисекунд гасим его в ноль.
STEAL_SECONDS = 0.008


class Synth:
    """Полифонический синтезатор: 12-TET, синус с огибающей."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        # Фиксированная громкость: сила нажатия не учитывается, каждая нота
        # звучит одинаково громко. Нужно, когда важна ровность партии, а не
        # динамика, и когда клавиатура передаёт velocity неровно.
        self.fixed_velocity = False
        self._lock = threading.Lock()
        self._voices: dict[int, dict] = {}
        self._ghosts: list[dict] = []

    def note_on(self, note: int, velocity: int = 100) -> None:
        if self.fixed_velocity:
            velocity = 127
        freq = 440.0 * (2.0 ** ((note - 69) / 12.0))
        level = max(0.05, min(1.0, velocity / 127.0)) * 0.22
        with self._lock:
            voice = self._voices.get(note)
            if voice is not None:
                # Ту же ноту взяли снова, не отпустив: фазу не сбрасываем,
                # иначе на стыке двух нот слышен щелчок.
                voice["level"] = level
                voice["released"] = False
                voice["release_step"] = 0.0
                return
            if len(self._voices) >= MAX_VOICES:
                # Крадём самую старую ноту: у неё больше шансов быть уже
                # отпущенной, и её уход меньше заметен.
                oldest = max(self._voices,
                             key=lambda k: self._voices[k]["age"])
                gone = self._voices.pop(oldest)
                gone["released"] = True
                # Скорость спада фиксируется в момент кражи: иначе она
                # пересчитывалась бы от остатка на каждом блоке и голос
                # затухал бы бесконечно, с изломом на каждом стыке.
                gone["release_step"] = (
                    max(float(gone["env"]), 0.0)
                    / (STEAL_SECONDS * float(self.sample_rate)))
                self._ghosts.append(gone)
            self._voices[note] = {
                "freq": freq,
                "level": level,
                "age": 0,
                "released": False,
                "release_step": 0.0,
                # Фаза живёт внутри голоса: на стыке блоков она должна
                # продолжаться, иначе волна рвётся и слышен хрип.
                "phase": 0.0,
                # Огибающая тоже живёт в голосе: атака, ровный звук и
                # отпускание идут без скачков амплитуды.
                "env": 0.0,
            }

    def note_off(self, note: int) -> None:
        with self._lock:
            voice = self._voices.get(note)
            if voice is not None:
                voice["released"] = True
                # Спад идёт с постоянной скоростью от текущей амплитуды до
                # нуля за RELEASE_SECONDS — без ступенек на стыках блоков.
                voice["release_step"] = (
                    max(float(voice["env"]), 0.0)
                    / (RELEASE_SECONDS * float(self.sample_rate)))

    def _env(self, voice: dict, frames: int) -> np.ndarray:
        """Амплитуда на каждом отсчёте блока: линейный подъём и спад."""
        sr = float(self.sample_rate)
        start = float(voice.get("env", 0.0))
        n = np.arange(frames, dtype=np.float64)
        if voice.get("released"):
            step = float(voice.get("release_step") or 0.0)
            if step <= 0.0:
                arr = np.zeros(frames, dtype=np.float64)
            else:
                arr = start - step * n
                np.clip(arr, 0.0, 1.0, out=arr)
        else:
            step = 1.0 / max(1.0, ATTACK_SECONDS * sr)
            arr = np.clip(start + step * n, 0.0, 1.0)
        voice["env"] = float(arr[-1]) if frames else start
        return arr

    def render(self, frames: int) -> np.ndarray:
        out = np.zeros(frames, dtype=np.float32)
        with self._lock:
            if not self._voices and not self._ghosts:
                return out
            sr = float(self.sample_rate)
            t = np.arange(frames, dtype=np.float64) / sr
            two_pi = 2.0 * math.pi
            for voice in list(self._voices.values()):
                voice["age"] += frames
                env = self._env(voice, frames)
                wave = np.sin(two_pi * voice["freq"] * t + voice["phase"])
                out += (wave * env * voice["level"]).astype(np.float32)
                voice["phase"] = (voice["phase"]
                                  + two_pi * voice["freq"] * frames / sr
                                  ) % two_pi
            finished = [n for n, v in self._voices.items()
                        if v.get("released") and v["env"] <= 0.0]
            for note in finished:
                del self._voices[note]
            for ghost in list(self._ghosts):
                env = self._env(ghost, frames)
                wave = np.sin(two_pi * ghost["freq"] * t + ghost["phase"])
                out += (wave * env * ghost["level"]).astype(np.float32)
                ghost["phase"] = (ghost["phase"]
                                  + two_pi * ghost["freq"] * frames / sr
                                  ) % two_pi
                if ghost["env"] <= 0.0:
                    self._ghosts.remove(ghost)
        # Мягкий ограничитель вместо жёсткого клиппинга: аккорд из многих нот
        # больше не упирается в потолок и не превращается в хрип. На тихом
        # уровне tanh почти не меняет звук.
        return np.tanh(out).astype(np.float32)


class MidiInput:
    """Слушает выбранный MIDI-порт и кормит синтезатор."""

    def __init__(self, synth: Synth, log=None):
        self.synth = synth
        self.log = log or (lambda *a: None)
        self._midi = None
        self._port_name = ""

    def open(self, port_name: str = "") -> bool:
        if rtmidi is None:
            self.log("Модуль python-rtmidi не установлен — MIDI недоступен.")
            return False
        try:
            midi = rtmidi.MidiIn()
            ports = midi.get_ports()
            if not ports:
                self.log("MIDI-устройства не найдены.")
                return False
            names = [str(p) for p in ports]
            index = 0
            if port_name:
                chosen = pick_port(port_name, names)
                if not chosen:
                    # Сохранённого устройства сейчас нет. Молча играть в
                    # другое не нужно: это не тот инструмент.
                    self.log(f"MIDI-устройство «{port_name}» не найдено. "
                             f"Доступны: {', '.join(names)}.")
                    return False
                index = names.index(chosen)
            midi.open_port(index)
            midi.set_callback(self._on_message)
            try:
                midi.ignore_types(sysex=True, timing=True, active_sense=True)
            except Exception:
                pass
            self._midi = midi
            self._port_name = names[index]
            self.log(f"MIDI открыт: {self._port_name}")
            return True
        except Exception as exc:
            self.log(f"Ошибка MIDI: {exc}")
            return False

    @property
    def port_name(self) -> str:
        """Имя открытого порта. Пусто — ничего не открыто."""
        return self._port_name

    def _on_message(self, event, data=None) -> None:
        message = event[0] if isinstance(event, tuple) else event
        if not message:
            return
        status = message[0] & 0xF0
        if status == 0x90 and len(message) >= 3:
            velocity = message[2]
            if velocity > 0:
                self.synth.note_on(message[1], velocity)
            else:
                self.synth.note_off(message[1])
        elif status == 0x80 and len(message) >= 2:
            self.synth.note_off(message[1])

    def close(self) -> None:
        if self._midi is not None:
            try:
                self._midi.close_port()
            except Exception:
                pass
            self._midi = None
        self._port_name = ""
