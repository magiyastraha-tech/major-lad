"""Эффекты плеера: скорость и тон.

Скорость меняет длительность трека, но не его высоту. Основной способ —
склейка похожих кусков (WSOLA): программа ищет в исходном звуке место,
которое продолжает предыдущий кусок так же, как он продолжился бы сам,
и ставит его внахлёст. Для гитары и голоса это главное: у щипка и у
согласной сохраняется удар, а тянущаяся нота не начинает «качаться» и
звенеть, как у фазового вокодера. Вокодер остаётся запасным — на очень
сильных растяжениях, где склейка начинает повторять куски слишком навязчиво.

Фазовый вокодер, которым считалось раньше, разбирает звук на короткие
кадры и раскладывает в спектр: у каждой частоты внутри кадра своя фаза, и
при растяжении программа продолжает её плавно. Фазы соседних частот вокруг
сильной гармоники привязываются к её фазе — это убирает «металл» старого
вокодера. На резких атаках фаза сбрасывается, чтобы удар не размазывался.

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

# Кадр 2048 при 48 кГц — около 43 мс: хватает разрешения по частоте, чтобы
# низкие струны не «плыли», и достаточно коротко, чтобы звук не запаздывал.
_FRAME = 2048
# Шаг анализа — четверть кадра. Синтез перекрывает кадры ещё плотнее,
# поэтому окно Ханна в квадрате складывается ровно, без бугров.
_HOP = _FRAME // 4
# Порог поиска пиков: всё, что тише одного процента от самой громкой
# частоты кадра, за гармонику не считаем.
_PEAK_RATIO = 0.01


def _hann(size: int) -> np.ndarray:
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(size) / size)).astype(
        np.float32)


def _peaks(mag: np.ndarray) -> np.ndarray:
    """Индексы настоящих максимумов спектра — будущие гармоники.

    Сравниваем не с соседями вплотную, а через одну частоту: у окна Ханна
    по бокам от каждой гармоники есть собственные маленькие горбики, и
    если принимать их за отдельные гармоники, главный лепесток развалится
    на куски с разными фазами — звук станет тише и задрожит.
    """
    if mag.size < 5:
        return np.zeros(0, dtype=np.int64)
    idx = np.arange(2, mag.size - 2)
    if idx.size == 0:
        return np.zeros(0, dtype=np.int64)
    middle = mag[idx]
    is_peak = ((middle >= mag[idx - 1]) & (middle > mag[idx - 2])
               & (middle >= mag[idx + 1]) & (middle > mag[idx + 2]))
    idx = idx[is_peak]
    if idx.size == 0:
        return idx
    return idx[mag[idx] > float(mag.max()) * _PEAK_RATIO]


# Склейка похожих кусков (WSOLA). Работает на паре «кадр — половина кадра»:
# кадр 2048 при 48 кГц — те же 43 мс, что у вокодера, а шаг 1024 даёт
# перекрытие вполовину, на котором окно Ханна складывается ровно.
_WSOLA_HOP = _FRAME // 2
# Насколько далеко от расчётного места разрешено сдвинуться в поисках
# похожего куска: ±5 мс. Этого хватает, чтобы попасть в ту же фазу даже
# низкой струны, и мало, чтобы не уехать в соседнюю долю.
_WSOLA_TOL = _FRAME // 8
# Длина куска для сравнения — 21 мс: покрывает период басовой струны,
# поэтому склейка идёт по колебанию, а не по случайному совпадению.
_WSOLA_CMP = _FRAME // 2
# За этими пределами склейка начинает слишком явно повторять куски —
# там лучше ровный, хотя и «размазанный» вокодер.
_WSOLA_MIN = 0.5
_WSOLA_MAX = 1.75


def _similarity(segment: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Насколько каждый кусок segment похож на target, от -1 до 1.

    Сравнение по косинусу: корреляция делится на громкости обоих кусков,
    иначе громкое место всегда «похоже» на тихое просто потому, что больше.
    Считается через преобразование Фурье — для трёхминутного трека это
    втрое быстрее прямого перебора.
    """
    size = segment.size
    n = 1
    while n < size + target.size:
        n *= 2
    corr = np.fft.irfft(np.fft.rfft(segment, n)
                        * np.fft.rfft(target[::-1], n), n)
    corr = corr[target.size - 1:size]
    squares = np.concatenate(([0.0], np.cumsum(
        segment.astype(np.float64) ** 2)))
    energy = squares[target.size:] - squares[:size - target.size + 1]
    target_energy = max(float(np.dot(target, target)), 1e-9)
    return corr / np.sqrt(np.maximum(energy * target_energy, 1e-9))


def _wsola(x: np.ndarray, rate: float) -> np.ndarray:
    """Растяжение склейкой похожих кусков.

    Для каждого следующего куска программа смотрит, чем естественно
    продолжился бы прошлый, и берёт в исходнике кусок, который звучит
    так же — даже если он стоит на пару миллисекунд раньше или позже
    расчётного места. Поэтому склейка попадает в ту же фазу колебания,
    и на стыке не слышно щелчка и не появляется дрожания.
    """
    x = np.asarray(x, dtype=np.float32)
    n = x.size
    hop = _WSOLA_HOP
    tol = _WSOLA_TOL
    cmp_len = _WSOLA_CMP
    win = _hann(_FRAME)
    out_len = max(1, int(round(n / rate)))
    pad = _FRAME + 2 * tol + _FRAME
    padded = np.concatenate((np.zeros(pad, dtype=np.float32), x,
                             np.zeros(pad, dtype=np.float32)))
    out = np.zeros(out_len + _FRAME + hop, dtype=np.float32)
    prev = -1
    pos = 0
    while pos < out_len:
        nominal = int(round(pos * rate))
        if prev < 0:
            chosen = nominal
        else:
            target = padded[pad + prev + hop:pad + prev + hop + cmp_len]
            start = nominal - tol
            segment = padded[pad + start:pad + start + 2 * tol + cmp_len]
            if float(np.dot(target, target)) < 1e-8:
                # Тишина: продолжать нечего, идём ровно по расчёту.
                chosen = nominal
            else:
                scores = _similarity(segment, target)
                chosen = start + int(np.argmax(scores))
        out[pos:pos + _FRAME] += padded[pad + chosen:pad + chosen + _FRAME] * win
        prev = chosen
        pos += hop
    return out[:out_len]


# Способы растяжения. «vocoder» — то, что звучало до версии 0.4.8: ровнее
# на всём диапазоне, но на гитарных атаках чуть смазаннее. «wsola» —
# склейка похожих кусков: атаки чётче, на больших растяжениях слышны
# повторы. По умолчанию стоит «vocoder»: владелец сравнил оба и выбрал его.
ALGOS = ("vocoder", "wsola")
DEFAULT_ALGO = "vocoder"


def time_stretch(x: np.ndarray, rate: float,
                 algo: str = DEFAULT_ALGO) -> np.ndarray:
    """Растягивает или сжимает звук по времени, не меняя высоту.

    rate — во сколько раз быстрее должен звучать трек: 1.1 — на десять
    процентов быстрее, 0.9 — медленнее. algo — способ растяжения.
    """
    rate = float(rate)
    if rate <= 0:
        raise ValueError("Скорость должна быть больше нуля.")
    x = np.asarray(x, dtype=np.float32)
    if abs(rate - 1.0) < 1e-3 or x.size < _FRAME * 2:
        return x.copy()
    # Склейка похожих кусков годится только в своём диапазоне скоростей:
    # дальше она повторяет куски слишком заметно, и звук ведёт вокодер.
    if algo == "wsola" and _WSOLA_MIN <= rate <= _WSOLA_MAX:
        return _wsola(x, rate)
    return _phase_vocoder(x, rate)


def _phase_vocoder(x: np.ndarray, rate: float) -> np.ndarray:
    """Растяжение фазовым вокодером — запасной способ.

    Звук разбирается на короткие кадры и раскладывается в спектр, а фаза
    каждой частоты продолжается плавно. На сильных растяжениях это ровнее
    склейки: куски не повторяются, звук просто становится матовее.
    """
    frame, hop = _FRAME, _HOP
    # Шаг синтеза: во столько раз реже (или чаще) ставим кадры на выходе.
    out_hop = max(1, int(round(hop / rate)))
    win = _hann(frame)
    # По краям добавляем тишину: первый и последний кадр должны быть
    # полными, иначе окно обрежет звук.
    padded = np.concatenate((np.zeros(frame, dtype=np.float32), x,
                             np.zeros(frame, dtype=np.float32)))

    out_len = max(1, int(round(x.size / rate)))
    total = out_len + frame
    out = np.zeros(total, dtype=np.float32)
    norm = np.zeros(total, dtype=np.float32)

    bins = frame // 2 + 1
    expected = (2.0 * np.pi * hop * np.arange(bins, dtype=np.float64)
                / frame)
    # Накопленная фаза синтеза и фаза прошлого кадра для сравнения.
    phase_acc = np.zeros(bins, dtype=np.float64)
    prev_phase = np.zeros(bins, dtype=np.float64)
    prev_mag = np.zeros(bins, dtype=np.float64)
    # Средняя «резкость» спектра: по ней видно атаку.
    flux_avg = 0.0

    starts = range(0, padded.size - frame + 1, hop)
    for index, start in enumerate(starts):
        pos = index * out_hop
        if pos >= total:
            break
        block = padded[start:start + frame] * win
        spec = np.fft.rfft(block)
        mag = np.abs(spec)
        phase = np.angle(spec)

        # Насколько фаза ушла от ожидаемой по номеру частоты. Разница
        # заворачивается в полупериод: скачок на целый оборот — это не
        # другая частота, а тот же звук.
        delta = phase - prev_phase - expected
        delta = (delta + np.pi) % (2.0 * np.pi) - np.pi
        true_freq = expected + delta
        phase_acc += true_freq * (out_hop / hop)

        # Резкая атака: спектр стал заметно богаче предыдущего кадра.
        # На таком кадре фазу не продолжаем, а берём из исходника как есть —
        # иначе щипок превращается в «шшш» перед ударом.
        flux = float(np.sum(np.maximum(mag - prev_mag, 0.0)))
        onset = flux > flux_avg * 2.0 and flux_avg > 0.0
        flux_avg = flux if not flux_avg else flux_avg * 0.9 + flux * 0.1
        if onset:
            phase_acc = phase.copy()

        out_phase = phase_acc.copy()
        peaks = _peaks(mag)
        # Даже одна гармоника — уже повод привязать к ней весь кадр: иначе
        # соседние частоты главного лепестка уходят каждая своим ходом,
        # лепесток рассыпается и звук проседает по громкости.
        if peaks.size > 0:
            # Границы участков — посередине между гармониками. Внутри
            # участка все частоты двигаются вместе с гармоникой, поэтому
            # тембр не рассыпается на отдельные «звенящие» бины.
            edges = np.empty(peaks.size + 1, dtype=np.int64)
            edges[0] = 0
            edges[1:-1] = (peaks[:-1] + peaks[1:]) // 2
            edges[-1] = bins
            for j, peak in enumerate(peaks):
                lo, hi = int(edges[j]), int(edges[j + 1])
                if hi <= lo:
                    continue
                out_phase[lo:hi] = (phase_acc[peak]
                                    + (phase[lo:hi] - phase[peak]))

        spectrum = mag * np.exp(1j * out_phase)
        piece = np.fft.irfft(spectrum, frame).astype(np.float32) * win
        end = min(pos + frame, total)
        part = end - pos
        out[pos:end] += piece[:part]
        norm[pos:end] += win[:part] * win[:part]

        prev_phase = phase
        prev_mag = mag

    result = np.zeros(out_len, dtype=np.float32)
    used = min(out_len, total)
    weight = norm[:used]
    result[:used] = np.divide(out[:used], weight,
                              out=np.zeros(used, dtype=np.float32),
                              where=weight > 1e-6)
    # На шумных местах (шипящие, выдох, овации) кадры складываются не так
    # плотно, как чистые тона, и трек звучит тише. Выравниваем общую
    # громкость, но не больше чем на треть: сильнее — это уже подмена
    # динамики трека.
    in_rms = float(np.sqrt(np.mean(x * x)))
    out_rms = float(np.sqrt(np.mean(result * result)))
    if in_rms > 1e-5 and out_rms > 1e-5:
        gain = min(1.3, max(0.7, in_rms / out_rms))
        if abs(gain - 1.0) > 0.01:
            result *= gain
    return result


def pitch_shift(x: np.ndarray, rate: int, semitones: float,
                algo: str = DEFAULT_ALGO) -> np.ndarray:
    """Сдвигает высоту на число полутонов, не меняя длительность."""
    semitones = float(semitones)
    if abs(semitones) < 1e-3:
        return np.asarray(x, dtype=np.float32).copy()
    factor = 2.0 ** (semitones / 12.0)
    stretched = time_stretch(x, 1.0 / factor, algo=algo)
    return resample(stretched, int(rate), max(1, int(round(int(rate) / factor))))


def apply(x: np.ndarray, rate: int = 48000,
          speed: float = 1.0, semitones: float = 0.0,
          algo: str = DEFAULT_ALGO) -> np.ndarray:
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
    stretched = time_stretch(x, speed / factor, algo=algo)
    if abs(factor - 1.0) > 1e-3:
        stretched = resample(stretched, int(rate),
                             max(1, int(round(int(rate) / factor))))
    return np.ascontiguousarray(np.clip(stretched, -1.0, 1.0), dtype=np.float32)
