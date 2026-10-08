from __future__ import annotations

import json
from typing import Optional

from PyQt6.QtCore import QMimeData

MIME_ASSET = "application/x-czeditor-asset"


def encode_asset_mime(items) -> QMimeData:
    if isinstance(items, tuple) and len(items) == 2 and isinstance(items[0], str):
        items = [items]
    data = QMimeData()
    payload = json.dumps({"items": [{"kind": k, "id": i} for k, i in items]}).encode("utf-8")
    data.setData(MIME_ASSET, payload)
    return data


def decode_asset_mime(mime_data: QMimeData) -> Optional[tuple]:
    items = decode_asset_mime_list(mime_data)
    return items[0] if items else None


def decode_asset_mime_list(mime_data: QMimeData) -> list:
    if not mime_data.hasFormat(MIME_ASSET):
        return []
    try:
        raw = bytes(mime_data.data(MIME_ASSET))
        payload = json.loads(raw.decode("utf-8"))
        items = payload.get("items")
        if items is None and "kind" in payload:  # tolerate the old single-asset payload
            items = [payload]
        return [(it["kind"], it["id"]) for it in (items or []) if it.get("kind") and it.get("id")]
    except Exception:
        return []
