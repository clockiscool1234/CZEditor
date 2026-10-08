from __future__ import annotations

import os
import sys


def _app_root() -> str:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def asset_path(*parts: str) -> str:
    return os.path.join(_app_root(), "assets", *parts)


def icon_path() -> str:
    if sys.platform == "win32":
        candidates = ["icon.ico", "icon.png", "icon.svg"]
    elif sys.platform == "darwin":
        candidates = ["icon.icns", "icon.png", "icon.svg"]
    else:
        candidates = ["icon.png", "icon.svg"]

    for name in candidates:
        path = asset_path(name)
        if os.path.isfile(path):
            return path
    return asset_path("icon.svg")
