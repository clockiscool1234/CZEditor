from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


class BackendError(RuntimeError):
    pass


class LocalBackend:
    def stop(self) -> None:
        pass

    def find_project_dirs(self, folder: str) -> list[str]:
        root = Path(folder)
        if not root.is_dir():
            return []
        try:
            names = [
                entry.name
                for entry in root.iterdir()
                if entry.is_dir() and (entry / "project.json").is_file()
            ]
        except OSError as e:
            raise BackendError(f"couldn't list {folder}: {e}") from e
        return sorted(names, key=str.lower)

    def read_text_file(self, path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise BackendError(f"couldn't read {path}: {e}") from e

    def save_text_file(self, path: str, contents: str) -> None:
        p = Path(path)
        try:
            if p.parent and not p.parent.is_dir():
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(contents, encoding="utf-8")
        except OSError as e:
            raise BackendError(f"couldn't write {path}: {e}") from e

    def make_temp_dir(self, prefix: str = "czeditor") -> str:
        try:
            return tempfile.mkdtemp(prefix=f"{prefix}-")
        except OSError as e:
            raise BackendError(str(e)) from e

    def remove_path(self, path: str) -> None:
        p = Path(path)
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except OSError as e:
            raise BackendError(str(e)) from e
