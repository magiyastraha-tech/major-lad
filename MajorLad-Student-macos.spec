# -*- mode: python ; coding: utf-8 -*-
"""Сборка «Мажорного лада — ученик» для macOS.

Получается одно приложение MajorLad-Student.app. Две вещи, которых на
Windows не было:

* Микрофон и камера на Маке закрыты системой: приложение получает их,
  только если заранее объяснило, зачем они ему. Эти объяснения —
  NSMicrophoneUsageDescription и NSCameraUsageDescription — лежат в
  Info.plist; без них macOS закрывает программу в момент первого
  обращения к микрофону.
* Opus лежит отдельной библиотекой. Если в окружении сборки задан
  OPUS_LIB (путь к libopus.dylib), она кладётся внутрь приложения и
  находится там при запуске; нет библиотеки — звук идёт без сжатия.
"""
import os

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
# av — видео кодеком H.264: внутри пакета libx264 и декодер H.264,
# собранные под эту систему. Без пакета видео работает на JPEG.
for _package in ("sounddevice", "rtmidi", "cv2", "soundfile", "av",
                 "PySide6.QtMultimedia"):
    _d, _b, _h = collect_all(_package)
    datas += _d
    binaries += _b
    hiddenimports += _h

_opus = os.environ.get("OPUS_LIB", "")
if _opus and os.path.exists(_opus):
    binaries.append((_opus, "."))

a = Analysis(
    ["main_student.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

# На Маке приложение собирается папкой: EXE, затем COLLECT в папку и
# BUNDLE, который заворачивает папку в .app. Папка запускается заметно
# быстрее одного файла — файл при каждом старте распаковывается заново.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MajorLad-Student",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MajorLad-Student",
)

# Версия в окне «Об этом приложении» на Маке. Берём её из пакета,
# чтобы не держать второй список версий в spec-файле.
_version = "0.0.0"
with open("gcapp/__init__.py", encoding="utf-8") as _f:
    for _line in _f:
        if _line.startswith("__version__"):
            _version = _line.split("=", 1)[1].strip().strip('"').strip("'")
            break

app = BUNDLE(
    coll,
    name="MajorLad-Student.app",
    icon=None,
    bundle_identifier="tech.magiyastraha.majorlad.student",
    info_plist={
        "CFBundleShortVersionString": _version,
        "CFBundleVersion": _version,
        "CFBundleDisplayName": "Мажорный лад ученик",
        "CFBundleName": "MajorLad-Student",
        "NSMicrophoneUsageDescription":
            "Микрофон нужен, чтобы преподаватель слышал ученика на уроке.",
        "NSCameraUsageDescription":
            "Камера нужна, чтобы преподаватель видел ученика на уроке.",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
