"""MIDI-вход и синтезатор.

Пользователь просил «MIDI-движок от Microsoft», чтобы клавиатура играла
и ученик это слышал. В Windows родной MIDI-движок — это MME/WinMM, и
доступ к нему в Python даёт python-rtmidi. Звук синтезируется здесь же:
синус, треугольник, квадрат или семплы из папки Data/Sampler — так не
нужны SoundFont-банки и лишние DLL, а сборка остаётся портативной в один
exe.

Огибающая у каждой ноты своя: атака, декай, сустейн и отпускание.
Громкость берётся из силы нажатия клавиши — или всегда полная, если
динамику выключили в настройках. «Педаль» — тоже настройка, а не
настоящая педаль: с ней ноты продолжают звучать после отпускания клавиши.
"""
from __future__ import annotations

import math
import re
import threading
import time
from pathlib import Path

import numpy as np

from .config import data_dir

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


def sampler_dir() -> Path:
    """Папка семплов: Data/Sampler рядом с программой.

    Создаётся при первом обращении, чтобы человеку было куда положить
    файлы. Номер файла — номер MIDI-ноты: 060.wav звучит на «до первой
    октавы», 069.wav — на «ля первой». Нет файла — эта нота молчит.
    """
    path = data_dir() / "Sampler"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


MAX_VOICES = 24
# Украденный голос не обрываем: за несколько миллисекунд гасим его в ноль.
STEAL_SECONDS = 0.008
# Семпл длиннее этого обрезаем: весь банк живёт в памяти, а клавиатурному
# звуку десяти секунд хватает с запасом.
SAMPLER_MAX_SECONDS = 10.0

WAVES = ("sine", "triangle", "square", "sampler")
DYNAMICS = ("fixed", "velocity")

# Громкость одной ноты для каждой формы волны: квадрат и семпл звучат
# плотнее синуса, поэтому им нужен меньший множитель.
_WAVE_GAIN = {"sine": 0.22, "triangle": 0.22, "square": 0.19, "sampler": 0.85}

DEFAULT_ATTACK_MS = 6.0
DEFAULT_DECAY_MS = 0.0
DEFAULT_SUSTAIN = 100.0
DEFAULT_RELEASE_MS = 45.0


def _polyblep(t: np.ndarray, dt: float) -> np.ndarray:
    """Сглаживание ступеньки: без него квадрат на высоких нотах звенит.

    Кусочек полинома, который сшивает разрыв периода с обеих сторон, —
    высокие гармоники выше половины частоты дискретизации не рождаются.
    """
    out = np.zeros_like(t)
    if dt <= 0.0:
        return out
    mask = t < dt
    if mask.any():
        x = t[mask] / dt
        out[mask] = 2.0 * x - x * x - 1.0
    mask = t > 1.0 - dt
    if mask.any():
        x = (t[mask] - 1.0) / dt
        out[mask] = x * x + 2.0 * x + 1.0
    return out


class SamplerBank:
    """Файлы Data/Sampler/NNN.wav: номер файла — номер MIDI-ноты.

    Нет файла для ноты — нота молчит: подставить соседний звук было бы
    хуже, чем не сыграть ничего. Файлы читаются при первом нажатии и
    остаются в памяти. Положил файл в папку при запущенной программе —
    он подхватится: неудачные попытки повторяются раз в несколько секунд.
    """

    def __init__(self, folder: str | Path, sample_rate: int):
        self.folder = Path(folder)
        self.sample_rate = int(sample_rate)
        self._cache: dict[int, np.ndarray | None] = {}
        self._miss_at: dict[int, float] = {}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def note(self, note: int) -> np.ndarray | None:
        """Моно float32 для ноты или None, если файла нет."""
        if note in self._cache:
            data = self._cache[note]
            if data is not None:
                return data
            if time.monotonic() - self._miss_at.get(note, 0.0) < 5.0:
                return None
        path = self.folder / f"{note:03d}.wav"
        data = None
        if path.is_file():
            try:
                from .media import load_track

                data = load_track(path, self.sample_rate)
                limit = int(SAMPLER_MAX_SECONDS * self.sample_rate)
                if data.size > limit:
                    data = data[:limit]
                if data.size == 0:
                    data = None
            except Exception:
                data = None
        self._cache[note] = data
        if data is None:
            self._miss_at[note] = time.monotonic()
        return data


class Synth:
    """Полифонический синтезатор с огибающей и выбором звука."""

    def __init__(self, sample_rate: int):
        self.sample_rate = int(sample_rate)
        self._lock = threading.Lock()
        self._voices: dict[int, dict] = {}
        self._ghosts: list[dict] = []
        self._wave = "sine"
        # «velocity» — громкость от силы нажатия, «fixed» — всегда полная.
        self._dynamics = "velocity"
        self._pedal = False
        self._attack_ms = DEFAULT_ATTACK_MS
        self._decay_ms = DEFAULT_DECAY_MS
        self._sustain = DEFAULT_SUSTAIN / 100.0
        self._release_ms = DEFAULT_RELEASE_MS
        self._bank: SamplerBank | None = None
        self._bank_dir = ""

    # -- настройки ----------------------------------------------------------

    def configure(self, wave: str | None = None,
                  dynamics: str | None = None,
                  pedal: bool | None = None,
                  attack_ms: float | None = None,
                  decay_ms: float | None = None,
                  sustain: float | None = None,
                  release_ms: float | None = None,
                  sampler_dir: str | Path | None = None) -> None:
        """Меняет звук на ходу, не обрывая уже играющие ноты.

        Сустейн приходит в процентах (0–100), времена — в миллисекундах.
        Выключили педаль — всё, что держалось педалью, отпускается и
        затухает как обычно.
        """
        with self._lock:
            if wave in WAVES:
                self._wave = str(wave)
            if dynamics in DYNAMICS:
                self._dynamics = str(dynamics)
            if attack_ms is not None:
                self._attack_ms = max(0.0, min(5000.0, float(attack_ms)))
            if decay_ms is not None:
                self._decay_ms = max(0.0, min(5000.0, float(decay_ms)))
            if sustain is not None:
                self._sustain = max(0.0, min(100.0, float(sustain))) / 100.0
            if release_ms is not None:
                self._release_ms = max(0.0, min(5000.0, float(release_ms)))
            folder = str(sampler_dir or "")
            if folder != self._bank_dir:
                self._bank_dir = folder
                self._bank = (SamplerBank(folder, self.sample_rate)
                              if folder else None)
            if pedal is not None:
                pedal = bool(pedal)
                if self._pedal and not pedal:
                    for voice in self._voices.values():
                        self._release(voice)
                self._pedal = pedal

    # -- ноты ---------------------------------------------------------------

    def note_on(self, note: int, velocity: int = 100) -> None:
        note = int(note) & 0x7F
        # Файл семпла читается с диска ДО захвата замка. Первое обращение
        # к файлу занимает десятки миллисекунд, а звуковой поток не должен
        # стоять и ждать: подожди он под замком — на выходе был бы провал
        # и щелчок.
        with self._lock:
            want_sample = self._wave == "sampler"
            bank = self._bank
        sample = None
        if want_sample and bank is not None:
            sample = bank.note(note)
        with self._lock:
            # Волну могли сменить, пока читался файл: тогда семпл не нужен.
            wave = self._wave
            if self._dynamics == "fixed":
                velocity = 127
            level = (max(0.05, min(1.0, float(velocity) / 127.0))
                     * _WAVE_GAIN.get(wave, 0.22))
            if wave != "sampler" or sample is None:
                sample = None
            if wave == "sampler" and sample is None:
                # Файла нет — нота молчит, как и просили.
                existing = self._voices.get(note)
                if existing is not None:
                    self._release(existing)
                return
            freq = 440.0 * (2.0 ** ((note - 69) / 12.0))
            voice = self._voices.get(note)
            if voice is not None:
                # Ту же ноту взяли снова, не отпустив: фазу не сбрасываем,
                # иначе на стыке двух нот слышен щелчок. Огибающая снова
                # идёт в атаку от текущего уровня.
                voice["level"] = level
                voice["wave"] = wave
                voice["sample"] = sample
                voice["pos"] = 0
                voice["done"] = False
                voice["released"] = False
                voice["release_step"] = 0.0
                voice["stage"] = "attack"
                return
            if len(self._voices) >= MAX_VOICES:
                # Крадём самую старую ноту: у неё больше шансов быть уже
                # отпущенной, и её уход меньше заметен.
                oldest = max(self._voices,
                             key=lambda k: self._voices[k]["age"])
                gone = self._voices.pop(oldest)
                gone["released"] = True
                gone["stage"] = "release"
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
                "stage": "attack",
                # Фаза живёт внутри голоса и нормирована в 0..1: на стыке
                # блоков она должна продолжаться, иначе волна рвётся.
                "phase": 0.0,
                "wave": wave,
                "sample": sample,
                "pos": 0,
                "done": False,
                # Огибающая тоже живёт в голосе: атака, декай, сустейн и
                # отпускание идут без скачков амплитуды.
                "env": 0.0,
            }

    def note_off(self, note: int) -> None:
        with self._lock:
            if self._pedal:
                # «Педаль нажата»: отпущенная клавиша продолжает звучать.
                return
            self._release(self._voices.get(int(note) & 0x7F))

    def _release(self, voice: dict | None) -> None:
        """Спад от текущей амплитуды до нуля за время релиза."""
        if voice is None or voice.get("released"):
            return
        voice["released"] = True
        voice["stage"] = "release"
        voice["release_step"] = (
            max(float(voice.get("env", 0.0)), 0.0)
            / (max(1.0e-4, self._release_ms / 1000.0)
               * float(self.sample_rate)))

    # -- звук ---------------------------------------------------------------

    def _env(self, voice: dict, frames: int) -> np.ndarray:
        """Амплитуда на каждом отсчёте: атака → декай → сустейн/релиз.

        Кусок блока, пока стадия не сменилась, считается целиком — без
        циклов по отсчётам: вычисление идёт в звуковом потоке, и лишняя
        работа здесь превращается в хрип.
        """
        sr = float(self.sample_rate)
        out = np.zeros(frames, dtype=np.float64)
        i = 0
        guard = 0
        while i < frames and guard < 8:
            guard += 1
            stage = voice.get("stage", "sustain")
            level = float(voice.get("env", 0.0))
            left = frames - i
            if stage == "release":
                step = float(voice.get("release_step") or 0.0)
                if step <= 0.0 or level <= 0.0:
                    voice["env"] = 0.0
                    break
                n = min(left, int(math.ceil(level / step)) + 1)
                arr = np.maximum(level - step * np.arange(n), 0.0)
                out[i:i + n] = arr
                voice["env"] = float(arr[-1])
                i += n
                if voice["env"] <= 0.0:
                    voice["env"] = 0.0
                    break
            elif stage == "attack":
                if self._attack_ms <= 0.0 or level >= 1.0:
                    voice["env"] = 1.0
                    voice["stage"] = "decay"
                    continue
                step = 1.0 / (self._attack_ms / 1000.0 * sr)
                n = min(left, int(math.ceil((1.0 - level) / step)) + 1)
                arr = np.minimum(level + step * np.arange(n), 1.0)
                out[i:i + n] = arr
                voice["env"] = float(arr[-1])
                i += n
                if voice["env"] >= 1.0:
                    voice["stage"] = "decay"
            elif stage == "decay":
                target = self._sustain
                if self._decay_ms <= 0.0 or level <= target:
                    voice["env"] = target
                    voice["stage"] = "sustain"
                    out[i:] = target
                    break
                step = (1.0 - target) / (self._decay_ms / 1000.0 * sr)
                n = min(left, int(math.ceil((level - target) / step)) + 1)
                arr = np.maximum(level - step * np.arange(n), target)
                out[i:i + n] = arr
                voice["env"] = float(arr[-1])
                i += n
                if voice["env"] <= target:
                    voice["env"] = target
                    voice["stage"] = "sustain"
            else:  # сустейн: уровень держится, пока нота не отпущена
                out[i:] = level
                break
        return out

    def _osc(self, voice: dict, frames: int) -> np.ndarray:
        """Синус, треугольник или квадрат за один блок."""
        dt = voice["freq"] / float(self.sample_rate)
        phase = (voice["phase"]
                 + dt * np.arange(frames, dtype=np.float64)) % 1.0
        wave = voice.get("wave", "sine")
        if wave == "triangle":
            y = 4.0 * np.abs(phase - 0.5) - 1.0
        elif wave == "square":
            y = np.where(phase < 0.5, 1.0, -1.0)
            y = y + _polyblep(phase, dt) - _polyblep((phase + 0.5) % 1.0, dt)
        else:
            y = np.sin(2.0 * math.pi * phase)
        voice["phase"] = float((voice["phase"] + dt * frames) % 1.0)
        return y

    @staticmethod
    def _sample_block(voice: dict, frames: int):
        """Кусок семпла. (звук, кончился) или None, если уже кончился."""
        sample = voice["sample"]
        pos = int(voice.get("pos") or 0)
        if pos >= sample.size:
            return None
        chunk = sample[pos:pos + frames]
        voice["pos"] = pos + int(chunk.size)
        if chunk.size >= frames:
            return chunk, False
        out = np.zeros(frames, dtype=np.float32)
        out[:chunk.size] = chunk
        return out, True

    def render(self, frames: int) -> np.ndarray:
        out = np.zeros(frames, dtype=np.float32)
        if frames <= 0:
            return out
        with self._lock:
            if not self._voices and not self._ghosts:
                return out
            for voice in list(self._voices.values()):
                voice["age"] += frames
                env = self._env(voice, frames)
                if voice.get("sample") is not None:
                    part = self._sample_block(voice, frames)
                    if part is None:
                        voice["done"] = True
                        continue
                    wave, ended = part
                    if ended:
                        voice["done"] = True
                else:
                    wave = self._osc(voice, frames)
                out += (wave * env * voice["level"]).astype(np.float32)
            finished = [n for n, v in self._voices.items()
                        if v.get("done")
                        or (v.get("released") and v.get("env", 0.0) <= 0.0)]
            for note in finished:
                del self._voices[note]
            for ghost in list(self._ghosts):
                env = self._env(ghost, frames)
                if ghost.get("sample") is not None:
                    part = self._sample_block(ghost, frames)
                    if part is None:
                        self._ghosts.remove(ghost)
                        continue
                    wave, _ended = part
                else:
                    wave = self._osc(ghost, frames)
                out += (wave * env * ghost["level"]).astype(np.float32)
                if ghost.get("env", 0.0) <= 0.0:
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
