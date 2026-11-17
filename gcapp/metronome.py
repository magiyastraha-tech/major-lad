"""Метроном.

Щелчки считаются прямо в звуковом потоке, а не таймером интерфейса: таймер
при загрузке системы плывёт на десятки миллисекунд, а звуковой блок приходит
ровно через каждые 480 отсчётов, поэтому темп не дрожит даже на медленной
машине. Метроном слышен и в наушниках, и ученику — он уходит в тот же
UDP-поток, что и остальной звук.

Доли считаются по размеру такта: первая доля громче и выше, остальные тише.
Частоты выбраны так, чтобы щелчок был слышен и на ноутбучных динамиках, и в
закрытых наушниках, но не резал слух.

Настройки меняются из интерфейса, а читаются из звукового потока. Замков
нет намеренно: enabled, bpm и beats — это bool и целые числа, их чтение и
запись в Python атомарны, а блокировка в звуковом потоке только добавила бы
риск щелчка.
"""
from __future__ import annotations

import math

import numpy as np

CLICK_SECONDS = 0.04
CLICK_LEVEL = 0.9
MIN_BPM = 20
MAX_BPM = 400


def _click(sample_rate: int, freq: float, level: float) -> np.ndarray:
    """Короткий тон с затуханием: острый щелчок без «писка» после него."""
    count = max(1, int(sample_rate * CLICK_SECONDS))
    t = np.arange(count, dtype=np.float64) / float(sample_rate)
    envelope = np.exp(-t * 55.0)
    return (np.sin(2.0 * math.pi * freq * t) * envelope * level).astype(np.float32)


class Metronome:
    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._accent = _click(sample_rate, 1500.0, CLICK_LEVEL)
        self._plain = _click(sample_rate, 1000.0, CLICK_LEVEL * 0.75)
        self.enabled = False
        self.bpm = 90
        self.beats = 4
        self._to_next = 0.0
        self._position: int | None = None
        self._sound = self._accent
        self._beat = 0

    def configure(self, enabled: bool, bpm: int = 90, beats: int = 4) -> None:
        was_on = self.enabled
        self.bpm = max(MIN_BPM, min(MAX_BPM, int(bpm or 90)))
        self.beats = max(1, min(16, int(beats or 4)))
        self.enabled = bool(enabled)
        if self.enabled and not was_on:
            # Включили — счёт начинается с первой доли, без полутакта ожидания.
            self._beat = 0
            self._to_next = 0.0
            self._position = None
        elif not self.enabled:
            self._position = None
            self._beat = 0
            self._to_next = 0.0

    def interval_samples(self) -> float:
        return self.sample_rate * 60.0 / float(self.bpm)

    def render(self, frames: int) -> np.ndarray:
        out = np.zeros(frames, dtype=np.float32)
        if not self.enabled:
            return out

        interval = self.interval_samples()
        pos = 0
        while pos < frames:
            if self._position is not None:
                take = min(frames - pos, len(self._sound) - self._position)
                out[pos:pos + take] += self._sound[self._position:self._position + take]
                self._position += take
                pos += take
                if self._position >= len(self._sound):
                    self._position = None
                continue
            if self._to_next <= 0.0:
                self._sound = self._accent if self._beat == 0 else self._plain
                self._beat = (self._beat + 1) % self.beats
                self._position = 0
                self._to_next += interval
                continue
            step = min(frames - pos, max(1, int(math.ceil(self._to_next))))
            pos += step
            self._to_next -= step
        return out
