#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SVG_PATH = os.path.join(ROOT, "assets", "icon.svg")
OUT_DIR = os.path.join(ROOT, "build_assets")

RENDER_SIZE = 1024
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def render_svg(svg_path: str, size: int):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QGuiApplication, QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer
    from PIL import Image

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        raise SystemExit(f"could not parse SVG: {svg_path}")

    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()

    buf = image.bits().asstring(image.sizeInBytes())
    pil_img = Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1)
    return app, pil_img


def main() -> None:
    if not os.path.isfile(SVG_PATH):
        raise SystemExit(f"icon source not found: {SVG_PATH}")

    os.makedirs(OUT_DIR, exist_ok=True)
    _app, base = render_svg(SVG_PATH, RENDER_SIZE)

    png_path = os.path.join(OUT_DIR, "icon.png")
    base.save(png_path)
    print(f"wrote {png_path}")

    ico_path = os.path.join(OUT_DIR, "icon.ico")
    base.save(ico_path, sizes=[(s, s) for s in ICO_SIZES])
    print(f"wrote {ico_path}")

    icns_path = os.path.join(OUT_DIR, "icon.icns")
    from PIL import Image

    resized = {s: base.resize((s, s), Image.LANCZOS) for s in ICNS_SIZES if s != RENDER_SIZE}
    resized[RENDER_SIZE] = base
    largest = resized[max(resized)]
    largest.save(icns_path, sizes=[(s, s) for s in sorted(resized)])
    print(f"wrote {icns_path}")


if __name__ == "__main__":
    main()
