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
import threading

import numpy as np

try:
    import rtmidi
except Exception:  # pragma: no cover
    rtmidi = None

MAX_VOICES = 24


def list_midi_ports() -> list[str]:
    if rtmidi is None:
        return []
    try:
        probe = rtmidi.MidiIn()
        return list(probe.get_ports())
    except Exception:
        return []


class Synth:
    """Полифонический синтезатор: 12-TET, синус с огибающей."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._voices: dict[int, dict] = {}
        self._phase = 0.0

    def note_on(self, note: int, velocity: int = 100) -> None:
        freq = 440.0 * (2.0 ** ((note - 69) / 12.0))
        with self._lock:
            if len(self._voices) >= MAX_VOICES:
                oldest = min(self._voices, key=lambda k: self._voices[k]["age"])
                self._voices.pop(oldest, None)
            self._voices[note] = {
                "freq": freq,
                "level": max(0.05, min(1.0, velocity / 127.0)) * 0.25,
                "age": 0,
                "released": False,
            }

    def note_off(self, note: int) -> None:
        with self._lock:
            voice = self._voices.get(note)
            if voice is not None:
                voice["released"] = True

    def render(self, frames: int) -> np.ndarray:
        out = np.zeros(frames, dtype=np.float32)
        if not self._voices:
            return out
        t = np.arange(frames, dtype=np.float64) / self.sample_rate
        with self._lock:
            finished = []
            for note, voice in self._voices.items():
                voice["age"] += frames
                amp = np.float32(voice["level"])
                if voice["released"]:
                    amp = amp * 0.85
                    if amp < 0.004:
                        finished.append(note)
                        continue
                    voice["level"] = float(amp)
                wave = np.sin(2.0 * math.pi * voice["freq"] * t + self._phase)
                out += (wave * amp).astype(np.float32)
            for note in finished:
                del self._voices[note]
        # Фаза общая — так соседние блоки не дают щелчков на стыке.
        self._phase = (self._phase + 2.0 * math.pi * 440.0 * frames / self.sample_rate) % (2.0 * math.pi)
        return out


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
            index = 0
            if port_name:
                for i, name in enumerate(ports):
                    if name == port_name:
                        index = i
                        break
            midi.open_port(index)
            midi.set_callback(self._on_message)
            try:
                midi.ignore_types(sysex=True, timing=True, active_sense=True)
            except Exception:
                pass
            self._midi = midi
            self._port_name = ports[index]
            self.log(f"MIDI открыт: {self._port_name}")
            return True
        except Exception as exc:
            self.log(f"Ошибка MIDI: {exc}")
            return False

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
