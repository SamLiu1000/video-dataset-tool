"""轻量设置持久化：保存到程序文件夹下的 vct_settings.json。

为什么不用 QSettings(注册表)：本工具是绿色/便携版，设置应随程序目录走，
换机器/解压即可用。冻结(绿色版)文件放在 exe 所在目录；开发模式放在项目根目录。
兼容旧版本：json 不存在(首次运行)时，把以前 QSettings 存到注册表的值迁移过来。
"""
import json
import os
import sys
import threading

_LOCK = threading.RLock()
_DATA: dict | None = None
_FILE = ""


def _settings_file() -> str:
    if getattr(sys, "frozen", False):
        # 绿色版：exe 所在目录（dist\\VideoCropTool\\ 或用户解压后的目录）
        base = os.path.dirname(sys.executable)
    else:
        # 开发模式：项目根目录（video_crop_tool 包的上一级）
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "vct_settings.json")


def _load() -> dict:
    global _DATA, _FILE
    if _DATA is not None:
        return _DATA
    _FILE = _settings_file()
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            _DATA = json.load(f) or {}
    except Exception:
        _DATA = {}
    if not _DATA:
        _migrate_registry()
    return _DATA


def _save_locked() -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(_DATA, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _migrate_registry() -> None:
    """首启把旧的 QSettings(注册表)值迁到 json，避免升级后丢设置。"""
    try:
        from PySide6.QtCore import QSettings
        q = QSettings("video_crop_tool", "video_crop_tool")
        out = {}
        for k in ("lang", "presets", "out_dir"):
            v = q.value(k)
            if v is not None:
                out[k] = v
        if out:
            _DATA.clear()
            _DATA.update(out)
            _save_locked()
    except Exception:
        pass


def get(key: str, default=None):
    return _load().get(key, default)


def set(key: str, value) -> None:
    with _LOCK:
        _load()
        _DATA[key] = value
        _save_locked()
