"""Эффекты плеера: скорость и тон.

Скорость меняет длительность трека, но не его высоту: звук растягивается
методом WSOLA. Кадры в 21 миллисекунду склеиваются не подряд, а по месту,
где продолжение звука похоже на то, что должно было идти дальше, — поэтому
нет эха и дрожания.

Тон меняется наоборот: звук сначала растягивается на нужную долю, а потом
проигрывается с другой частотой. Длительность возвращается к прежней, а
высота сдвигается на заданное число полутонов.

Обработка идёт в фоне при нажатии кнопки, а не во время воспроизведения:
трек уже целиком лежит в памяти, так что пересчитать его один раз проще и
надёжнее, чем тянуть эффект в звуковом потоке.
"""
from __future__ import annotations

import numpy as np

from .media import resample

# Кадр около 21 мс при 48 кГц: короткий настолько, чтобы склейки не были
# слышны, и длинный настолько, чтобы программа успевала найти похожее место.
_FRAME = 1024
# Насколько далеко от нужного места искать похожее продолжение.
_TOLERANCE = 192


def _best_start(x: np.ndarray, target: int, template: np.ndarray,
                tolerance: int) -> int:
    """Ищет начало кадра рядом с target, продолжение которого похоже на template."""
    lo = max(0, target - tolerance)
    hi = min(target + tolerance, x.size - template.size)
    if hi <= lo:
        return max(0, min(target, x.size - template.size))
    region = x[lo:hi + template.size]
    size = 1
    while size < region.size + template.size:
        size <<= 1
    # Взаимная корреляция через преобразование Фурье: на кадр уходит
    # несколько десятков микросекунд вместо миллионов сложений.
    corr = np.fft.irfft(np.fft.rfft(region, size)
                        * np.conj(np.fft.rfft(template, size)), size)
    count = hi - lo + 1
    return lo + int(np.argmax(corr[:count]))


def time_stretch(x: np.ndarray, rate: float,
                 frame: int = _FRAME, tolerance: int = _TOLERANCE) -> np.ndarray:
    """Растягивает или сжимает звук по времени, не меняя высоту.

    rate — во сколько раз быстрее должен звучать трек: 1.1 — на десять
    процентов быстрее, 0.9 — медленнее.
    """
    rate = float(rate)
    if rate <= 0:
        raise ValueError("Скорость должна быть больше нуля.")
    x = np.asarray(x, dtype=np.float32)
    if abs(rate - 1.0) < 1e-3 or x.size < frame * 3:
        return x.copy()

    hop = frame // 2
    # Периодическая оконная функция Ханна: соседние кадры со сдвигом в
    # половину кадра складываются ровно в единицу.
    win = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(frame) / frame)).astype(np.float32)
    # Хвост исходника дополняем тишиной, чтобы последний кадр не оборвался.
    padded = np.concatenate((x, np.zeros(frame, dtype=np.float32)))

    out_len = int(round(x.size / rate))
    total_len = out_len + frame
    out = np.zeros(total_len, dtype=np.float32)
    weight = np.zeros(total_len, dtype=np.float32)

    step = hop * rate
    pos = 0
    index = 0
    while True:
        seg = padded[pos:pos + frame]
        if seg.size < frame:
            break
        start = index * hop
        if start >= total_len:
            break
        end = min(start + frame, total_len)
        part = end - start
        out[start:end] += seg[:part] * win[:part]
        weight[start:end] += win[:part]
        index += 1
        target = pos + int(round(step))
        if target + frame >= padded.size:
            break
        # Продолжение предыдущего кадра в исходнике — по нему ищем, где
        # начать следующий кадр, чтобы фаза не сбилась.
        template = padded[pos + hop:pos + frame]
        pos = _best_start(padded, target, template, tolerance)

    result = np.zeros(out_len, dtype=np.float32)
    used = min(out_len, out.size)
    weight_part = weight[:used]
    result[:used] = np.divide(out[:used], weight_part,
                              out=np.zeros(used, dtype=np.float32),
                              where=weight_part > 1e-6)
    return result


def pitch_shift(x: np.ndarray, rate: int, semitones: float) -> np.ndarray:
    """Сдвигает высоту на число полутонов, не меняя длительность."""
    semitones = float(semitones)
    if abs(semitones) < 1e-3:
        return np.asarray(x, dtype=np.float32).copy()
    factor = 2.0 ** (semitones / 12.0)
    stretched = time_stretch(x, 1.0 / factor)
    return resample(stretched, int(rate), max(1, int(round(int(rate) / factor))))


def apply(x: np.ndarray, rate: int = 48000,
          speed: float = 1.0, semitones: float = 0.0) -> np.ndarray:
    """Готовит звук с текущими скоростью и тоном.

    Оба эффекта сводятся к одному растяжению и одному пересчёту частоты:
    скорость задаёт, во сколько раз короче станет трек, а тон — на какую
    долю потом сжать его обратно ради высоты.
    """
    x = np.asarray(x, dtype=np.float32)
    speed = float(speed)
    semitones = float(semitones)
    if abs(speed - 1.0) < 1e-3 and abs(semitones) < 1e-3:
        return x.copy()
    factor = 2.0 ** (semitones / 12.0)
    stretched = time_stretch(x, speed / factor)
    if abs(factor - 1.0) > 1e-3:
        stretched = resample(stretched, int(rate),
                             max(1, int(round(int(rate) / factor))))
    return np.ascontiguousarray(np.clip(stretched, -1.0, 1.0), dtype=np.float32)
