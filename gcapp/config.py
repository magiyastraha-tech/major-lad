"""Конфигурация программы «Мажорный лад».

Файл настроек лежит в папке Data рядом с программой — это делает сборку
портативной: скопировал папку на другой компьютер, настройки переехали
вместе с ней.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

# Отметка «системное устройство по умолчанию» в настройках звука. На
# другом компьютере имена устройств другие, а эта отметка всегда означает
# то, что Windows выбрала сама, — поэтому на новом месте звук заработает
# без захода в настройки.
DEFAULT_DEVICE = "__default__"

DEFAULTS = {
    # Версия схемы настроек: по ней load() понимает, какие старые
    # умолчания пора привести к новым.
    "settings_version": 7,
    "role": "",
    "name": "",
    "server_ip": "",
    "server_port": 45800,
    "autoconnect": True,
    "audio": {
        # Драйвер звука. По умолчанию WASAPI: на ноутбуке со встроенной
        # картой ничего настраивать не нужно. ASIO выбирают те, у кого есть
        # внешняя карта и её драйвер.
        "driver": "wasapi",
        "asio_device": "",
        "asio_in_channels": 2,
        "asio_out_channels": 2,
        "device1": DEFAULT_DEVICE,
        "device2": "",
        "usb": "",
        "output": "",
        # Режим канала входа: «ch1» — первый вход устройства, «ch2» — второй,
        # «stereo» — оба сразу (первый слева, второй справа).
        "mode1": "ch1",
        "mode2": "ch1",
        "usb_mode": "ch1",
        # Обработка первого входа: слышать себя в наушниках (мониторинг),
        # срез всего ниже 150 Гц и сила оптического компрессора в процентах
        # (0 — выключен).
        "monitor1": False,
        "lowcut": False,
        "compressor": 0,
        "midi_enabled": False,
        "midi_port": "",
        # Звук синтезатора: sine (синус), triangle, square или sampler —
        # wav-файлы из Data/Sampler, номер файла равен номеру ноты.
        "midi_wave": "sine",
        # fixed — нота всегда полной громкости, velocity — от силы нажатия.
        "midi_dynamics": "velocity",
        # True — «педаль нажата»: ноты звучат и после отпускания клавиши.
        "midi_pedal": False,
        # Огибающая: атака и декай в миллисекундах, сустейн в процентах,
        # релиз в миллисекундах.
        "midi_attack_ms": 6,
        "midi_decay_ms": 0,
        "midi_sustain": 100,
        "midi_release_ms": 45,
        # Показывать в списках все драйверы, а не только WASAPI.
        "show_all_drivers": False,
        "volumes": {
            "student": 80,
            "in1": 100,
            "in2": 100,
            "usb": 100,
            "midi": 80,
            "metronome": 60,
            "player": 80,
        },
        # Громкость, которую слышит собеседник. Отдельная от своей: регулятор
        # в режиме «Громкость ученика» меняет только его сторону.
        "volumes_peer": {
            "student": 80,
            "in1": 100,
            "in2": 100,
            "usb": 100,
            "midi": 80,
            "metronome": 60,
            "player": 80,
        },
        "volume_mode": "local",
        # Размер буфера обмена звуком: low (20–40 мс), normal (40–80 мс),
        # stable (80–160 мс). Чем больше, тем устойчивее связь и тем длиннее
        # задержка.
        "buffer": "normal",
        # Битрейт сжатия Opus в кбит/с: 96, 128, 192 или 320. Действует на
        # исходящий поток и только когда оба конца умеют Opus.
        "opus_bitrate": 96,
        # Компенсация задержки: клик, MIDI и трек уходят собеседнику с
        # задержкой тракта, чтобы совпасть с голосом. 0 — автоматически,
        # иначе значение в миллисекундах.
        "tx_delay_ms": 0,
    },
    # Сочетания клавиш на кнопки и регуляторы главного окна.
    "shortcuts": {},
    "player": {
        "skip": 5,
        "last": "",
    },
    "metronome": {
        "enabled": False,
        "bpm": 90,
        "beats": 4,
    },
    "video": {
        "enabled": False,
        "camera": 0,
        "fps": 15,
        "quality": 60,
        # Размер кадра и предел потока в кбит/с (0 — без предела).
        # По умолчанию поток ограничен: неограниченный JPEG забивает канал
        # и трещит вместе со звуком.
        "width": 640,
        "height": 480,
        "bitrate": 600,
        "show_remote": True,
    },
}


DATA_DIR = "Data"

# Что прежние версии держали россыпью рядом с программой. При первом
# запуске всё это переезжает в Data; список остаётся здесь, чтобы миграция
# читалась в одном месте с путями.
_LEGACY_DIRS = ("Media", "History", "Sound", "Student settings",
                "received")
_LEGACY_FILES = (
    "config.json", "MajorLad-Teacher.json", "MajorLad-Student.json",
    "History.txt", "majorlad.log", "majorlad.log.1", "error_log.txt",
)


def app_dir() -> Path:
    """Папка программы. Для собранного exe — папка exe, для исходников — корень проекта."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Рабочая папка программы: всё, что программа создаёт, живёт здесь.

    Рядом с exe остаются только сам exe и папка Data: скопировал папку
    программы на другой компьютер — вместе с ней переехали настройки,
    треки, история, звуки и громкости учеников.
    """
    path = app_dir() / DATA_DIR
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def _merge_tree(src: Path, dst: Path) -> None:
    """Переносит содержимое папки, не затирая уже существующие файлы."""
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        items = list(src.iterdir())
    except OSError:
        return
    for item in items:
        target = dst / item.name
        try:
            if item.is_dir() and target.is_dir():
                _merge_tree(item, target)
            elif not target.exists():
                item.replace(target)
        except OSError:
            pass
    try:
        if not any(src.iterdir()):
            src.rmdir()
    except OSError:
        pass


def migrate() -> list[str]:
    """Переносит рабочие папки и настройки прежних версий в Data.

    Запускается при старте, до чтения настроек. Повторный запуск ничего не
    портит: уже перенесённое остаётся на месте, а файл, который Windows не
    отдала (занят другой копией программы), переедет при следующем старте.
    Возвращает список имён — что перенесено в этот раз.
    """
    base = app_dir()
    data = data_dir()
    moved: list[str] = []
    if base == data:
        return moved
    for name in _LEGACY_DIRS:
        src = base / name
        if not src.is_dir():
            continue
        dst = data / name
        if not dst.exists():
            try:
                src.rename(dst)
                moved.append(name)
                continue
            except OSError:
                pass
        _merge_tree(src, dst)
    names = list(_LEGACY_FILES)
    if getattr(sys, "frozen", False):
        stem = f"{Path(sys.executable).stem}.json"
        if stem not in names:
            names.append(stem)
    for name in names:
        src = base / name
        dst = data / name
        if not src.is_file() or dst.exists():
            continue
        try:
            src.replace(dst)
            moved.append(name)
        except OSError:
            pass
    return moved


def config_path() -> Path:
    """Настройки лежат в папке Data.

    У сборки exe файл называется по имени exe: у «Преподавателя» и «Ученика»
    настройки разные, и их можно держать в одной папке.
    """
    if getattr(sys, "frozen", False):
        return data_dir() / f"{Path(sys.executable).stem}.json"
    return data_dir() / "config.json"


def _merge(base: dict, data: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (data or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    path = config_path()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cfg = _merge(DEFAULTS, data if isinstance(data, dict) else {})
    # До 0.3.9 видео по умолчанию шло без предела и забивало канал.
    # Переводим старые настройки один раз; дальше выбор человека из
    # «Настройки → Видео» не трогаем — даже «Без предела».
    if int(cfg.get("settings_version", 1) or 1) < 2:
        video = cfg.setdefault("video", {})
        if not int(video.get("bitrate", 0) or 0):
            video["bitrate"] = 600
    # 0.4.1: у ученика звук и видео должны работать сразу после обновления,
    # без похода в настройки. Пустой микрофон меняем на «системный по
    # умолчанию», камеру включаем. Сделанный человеком выбор не трогаем:
    # миграция выполняется один раз и помечает настройки новой версией.
    if int(cfg.get("settings_version", 1) or 1) < 3:
        if str(cfg.get("role") or "") == "student":
            audio_cfg = cfg.setdefault("audio", {})
            if not str(audio_cfg.get("device1") or "").strip():
                audio_cfg["device1"] = DEFAULT_DEVICE
            cfg.setdefault("video", {})["enabled"] = True
        cfg["settings_version"] = 3
    # 0.4.7: переключателя «чья сторона играет» больше нет. Минус и метроном
    # всегда играет тот, у кого они включены, и его звук уходит собеседнику.
    # Старый ключ убираем, чтобы он не всплывал в сохранённом файле.
    if int(cfg.get("settings_version", 1) or 1) < 4:
        cfg.get("audio", {}).pop("backing_source", None)
        cfg["settings_version"] = 4
    # 0.5.1: компенсация задержки считается по-новому. Старое ручное
    # значение подгоняли под прежнюю формулу, и теперь оно уводит метроном
    # от игры. Один раз возвращаем «Автоматически»; дальше ручной выбор
    # человека трогать нельзя.
    if int(cfg.get("settings_version", 1) or 1) < 5:
        cfg.setdefault("audio", {})["tx_delay_ms"] = 0
        cfg["settings_version"] = 5
    # 0.6: у синтезатора появились выбор звука, динамика, педаль и
    # огибающая. Прежний флажок «всегда полная громкость» переезжает в
    # выбор динамики, чтобы человеку не пришлось настраивать заново.
    if int(cfg.get("settings_version", 1) or 1) < 6:
        audio_cfg = cfg.setdefault("audio", {})
        # Ключ из старых настроек доживает до сюда только если он был в
        # файле: в новых умолчаниях его уже нет. Свежая установка его не
        # имеет и получает «с динамикой» из DEFAULTS.
        if "midi_fixed_velocity" in audio_cfg:
            audio_cfg["midi_dynamics"] = (
                "fixed" if audio_cfg.pop("midi_fixed_velocity") else "velocity")
        cfg["settings_version"] = 6
    # 0.5.6: у первого входа появились мониторинг, срез низа и компрессор.
    # Данные не переносим — новые ключи приходят из DEFAULTS; версию
    # помечаем, чтобы будущие миграции не делались повторно.
    if int(cfg.get("settings_version", 1) or 1) < 7:
        cfg["settings_version"] = 7
    return cfg


def save(cfg: dict) -> None:
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
