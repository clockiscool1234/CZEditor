from __future__ import annotations

import json
import os
import sys

_current_code = "english"
_english: dict[str, str] = {}
_current: dict[str, str] = {}


def _find_locales_dir() -> str:
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidate = os.path.join(exe_dir, "locale")
        if os.path.isdir(candidate):
            return candidate
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return os.path.join(meipass, "locale")

    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "locale"
    )


_LOCALES_DIR = _find_locales_dir()


def _load_file(code: str) -> dict[str, str]:
    path = os.path.join(_LOCALES_DIR, f"{code}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
    return {}


def init(code: str = "english") -> None:
    global _current_code, _english, _current
    _current_code = code
    _english = _load_file("english")
    if code == "english":
        _current = _english
    else:
        overlay = _load_file(code)
        _current = {**_english, **overlay}


def tr(key: str) -> str:
    v = _current.get(key) or _english.get(key)
    return v if v is not None else key


def available_languages() -> dict[str, str]:
    path = os.path.join(_LOCALES_DIR, "languages.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return dict(data)
    except Exception:
        pass
    return {"english": "English"}

init()
