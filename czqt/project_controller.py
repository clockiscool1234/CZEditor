from __future__ import annotations

import json
import os
import shutil
import threading
import time
import zipfile
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .backend import LocalBackend
from .model import MAIN_ID, Folder, Project, Scene, SymbolDef, new_uuid, now_ms, SCHEMA_VERSION


class SaveCancelled(Exception):
    pass

def folder_name_of(path: str) -> str:
    return os.path.basename(os.path.normpath(path))


def _write_file_atomic(path: str, contents: str) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(contents)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _same_dir(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class ProjectController(QObject):
    project_changed = pyqtSignal()
    dirty_changed = pyqtSignal(bool)
    newer_version_detected = pyqtSignal(int)  # emitted with project's schema_version when it's newer than app

    def __init__(self, backend: LocalBackend, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.backend = backend
        self.project: Project = Project.new_default()
        self.project_dir: Optional[str] = None
        self._dirty = False
        self.invalidate_source_cache: Optional[Callable[[str], None]] = None

    # state
    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self):
        if not self._dirty:
            self._dirty = True
            self.dirty_changed.emit(True)

    def mark_saved(self):
        if self._dirty:
            self._dirty = False
            self.dirty_changed.emit(False)

    # ids
    def _all_project_ids(self) -> set[str]:
        ids: set[str] = set(self.project.scenes.keys())
        ids.update(self.project.symbol_defs.keys())
        ids.update(self.project.sources.keys())
        ids.update(self.project.folders.keys())
        return ids

    def _fresh_id(self) -> str:
        new_id = new_uuid()
        assert new_id not in self._all_project_ids(), (
            f"generated id {new_id!r} unexpectedly collides with an existing project id"
        )
        return new_id

    # new
    def new_project(self, name: str, width: int, height: int, fps: int):
        self.project = Project.new_default()
        self.project.name = name or self.project.name
        self.project.width = width or self.project.width
        self.project.height = height or self.project.height
        self.project.fps = fps or self.project.fps
        self.project_dir = None
        self._dirty = True
        self.project_changed.emit()
        self.dirty_changed.emit(True)

    # open
    def find_project_dirs(self, root_folder: str) -> list[str]:
        return self.backend.find_project_dirs(root_folder)

    def open_project(self, project_dir: str):
        path = os.path.join(project_dir, "project.json")
        text = self.backend.read_text_file(path)
        data = json.loads(text)
        file_schema_version = data.get("schema_version") or 1
        self.project = Project.from_dict(data)
        self.project.name = folder_name_of(project_dir)  # the folder is the project's name
        self.project_dir = project_dir
        self._dirty = False
        self.project_changed.emit()
        self.dirty_changed.emit(False)
        if file_schema_version > SCHEMA_VERSION:
            self.newer_version_detected.emit(file_schema_version)

    # save
    def save_project(self, project_dir: Optional[str] = None, progress=None) -> str:
        old_dir = self.project_dir
        target = project_dir or old_dir
        if not target:
            raise ValueError("no project directory chosen yet")
        os.makedirs(target, exist_ok=True)
        if old_dir and not _same_dir(old_dir, target):
            self.copy_assets(old_dir, target, progress)
        self.project.name = folder_name_of(target)  # the folder is the project's name
        path = os.path.join(target, "project.json")
        contents = json.dumps(self.project.to_dict(), indent=2)
        _write_file_atomic(path, contents)
        self.project_dir = target
        self._write_thumbnail(target)
        self.mark_saved()
        return target

    def copy_assets(self, src_dir: str, dst_dir: str, progress=None) -> list:
        jobs, missing, total = [], [], 0
        for uuid_ext in self.project.sources:
            src = os.path.join(src_dir, uuid_ext)
            if not os.path.isfile(src):
                missing.append(uuid_ext)
                continue
            size = os.path.getsize(src)
            dst = os.path.join(dst_dir, uuid_ext)
            if os.path.isfile(dst) and os.path.getsize(dst) == size:
                continue
            jobs.append((uuid_ext, src, dst, size))
            total += size
        done = 0
        if progress:
            progress(0, total, "")
        for uuid_ext, src, dst, size in jobs:
            part = dst + ".part"  # a half-copied file must never look like a finished one
            try:
                with open(src, "rb") as fin, open(part, "wb") as fout:
                    while True:
                        chunk = fin.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        fout.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total, self.project.sources.get(uuid_ext, uuid_ext))
                shutil.copystat(src, part)
                os.replace(part, dst)
            except BaseException:
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise
        if missing:
            print(f"[save] {len(missing)} source file(s) not found in {src_dir}: {missing[:5]}")
        return missing

    def _write_thumbnail(self, target: str):
        import threading
        busy = self.__dict__.setdefault("_thumb_busy_dirs", set())
        if target in busy:
            return  # this folder's thumbnail is already being written
        busy.add(target)
        project = self.project
        width, height = project.width, project.height

        def work():
            cache = None
            try:
                from .compositor import ImageCache, render_frame
                scale = min(1.0, 320 / max(1, width))
                w = max(1, round(width * scale))
                h = max(1, round(height * scale))
                cache = ImageCache()
                img = render_frame(project, MAIN_ID, 0.0, cache, target, w, h)
                tmp = os.path.join(target, "thumbnail.tmp.png")
                if img.save(tmp, "PNG"):
                    os.replace(tmp, os.path.join(target, "thumbnail.png"))  # never leave a half-written file
            except Exception:
                pass
            finally:
                if cache is not None:
                    cache.clear(join=True)  # stop the video decoder threads this render started
                busy.discard(target)

        threading.Thread(target=work, name="czeditor-project-thumbnail", daemon=True).start()

    # convenience
    def main_scene(self):
        return self.project.scenes[MAIN_ID]

    def _unique_name(self, base: str, existing: set[str]) -> str:
        if base not in existing:
            return base
        import re
        m = re.match(r'^(.*?)(?:\s+\d+)?(\.[a-zA-Z0-9]+)?$', base)
        if m:
            stem = m.group(1).strip()
            ext = m.group(2) or ""
        else:
            stem, ext = base, ""
        i = 1
        while True:
            name = f"{stem} {i}{ext}"
            if name not in existing:
                return name
            i += 1

    # library: scenes
    def insert_new_scene(self, name: str, folder_id: Optional[str] = None) -> str:
        # Name collisions are only checked among scenes (not symbols/sources) within
        # the same folder the new scene is being created in.
        existing = {
            s.name for s in self.project.scenes.values()
            if (s.folder_id or "") == (folder_id or "")
        }
        name = self._unique_name(name, existing)
        sid = self._fresh_id()
        self.project.scenes[sid] = Scene(
            id=sid, name=name, background=self.project.default_bg, created=now_ms(),
            folder_id=folder_id or "",
        )
        self.mark_dirty()
        return sid

    # library: classes
    def create_class(self, name: str):
        self.project.ensure_class(name)
        self.mark_dirty()

    def delete_class(self, cls: str) -> int:
        members = [sid for sid, s in self.project.symbol_defs.items() if s.elem_class == cls]
        for sid in members:
            del self.project.symbol_defs[sid]
            for scene in self.project.scenes.values():
                scene.placements = [p for p in scene.placements if not (p.kind == "symbol" and p.ref == sid)]
        self.project.classes = [c for c in self.project.classes if c != cls]
        self.mark_dirty()
        return len(members)

    # library: symbols
    def insert_new_symbol(self, data: dict, folder_id: Optional[str] = None) -> str:
        eid = self._fresh_id()
        kwargs = dict(data)

        # Name collisions are only checked among symbols (not scenes/sources)
        # within the same folder the new symbol is being created in.
        existing = {
            s.name for s in self.project.symbol_defs.values()
            if (s.folder_id or "") == (folder_id or "")
        }
        name = self._unique_name(kwargs.get("name", "Symbol"), existing)
        kwargs["name"] = name
        if "elem_class" not in kwargs or kwargs["elem_class"] == data.get("name"):
            kwargs["elem_class"] = name

        kwargs["id"] = eid
        kwargs["folder_id"] = folder_id or ""
        kwargs["created"] = now_ms()
        self.project.symbol_defs[eid] = SymbolDef(**{
            k: v for k, v in kwargs.items() if k in SymbolDef.__dataclass_fields__
        })
        self.project.ensure_class(kwargs.get("elem_class", "generic"))
        self.mark_dirty()
        return eid

    # library: assets
    def delete_asset(self, kind: str, real_id: str):
        if kind == "scene":
            if real_id == MAIN_ID:
                raise ValueError("The Main Timeline can't be deleted.")
            self.project.scenes.pop(real_id, None)
            for scene in self.project.scenes.values():
                scene.placements = [p for p in scene.placements if not (p.kind == "scene" and p.ref == real_id)]
        elif kind == "symbol":
            self.project.symbol_defs.pop(real_id, None)
            for scene in self.project.scenes.values():
                scene.placements = [p for p in scene.placements if not (p.kind == "symbol" and p.ref == real_id)]
        elif kind == "source":
            self.project.sources.pop(real_id, None)
            self.project.source_created.pop(real_id, None)
            self.project.source_folders.pop(real_id, None)
            for scene in self.project.scenes.values():
                scene.placements = [p for p in scene.placements if not (p.kind == "sound" and p.ref == real_id)]
        self.mark_dirty()

    # library: folders
    def create_folder(self, category: str, parent_id: Optional[str], name: str) -> str:
        fid = self._fresh_id()
        self.project.folders[fid] = Folder(
            id=fid, name=name, category=category, parent=parent_id or None, created=now_ms()
        )
        self.mark_dirty()
        return fid

    def rename_folder(self, folder_id: str, name: str):
        f = self.project.folders.get(folder_id)
        if f and name and name != f.name:
            f.name = name
            self.mark_dirty()

    def items_in_folders(self, category: str, folder_ids: list) -> list:
        in_set = set(folder_ids)
        if category == "source":
            return [u for u, fid in self.project.source_folders.items() if fid in in_set]
        if category == "scene":
            return [sid for sid, s in self.project.scenes.items() if s.folder_id in in_set]
        if category == "symbol":
            return [sid for sid, s in self.project.symbol_defs.items() if s.folder_id in in_set]
        return []

    def delete_folder(self, folder_id: str) -> int:
        f = self.project.folders.get(folder_id)
        if not f:
            return 0
        folder_ids = self.project.folder_and_descendants(folder_id)
        items = self.items_in_folders(f.category, folder_ids)
        for item_id in items:
            self.delete_asset(f.category, item_id)
        for fid in folder_ids:
            self.project.folders.pop(fid, None)
        self.mark_dirty()
        return len(items)

    def item_folder(self, kind: str, real_id: str) -> str:
        if kind == "source":
            return self.project.source_folders.get(real_id, "")
        if kind == "scene":
            s = self.project.scenes.get(real_id)
            return s.folder_id if s else ""
        if kind == "symbol":
            s = self.project.symbol_defs.get(real_id)
            return s.folder_id if s else ""
        return ""

    def move_item_to_folder(self, kind: str, real_id: str, folder_id: Optional[str]):
        if kind == "scene" and real_id == MAIN_ID:
            return
        if folder_id and (folder_id not in self.project.folders or self.project.folders[folder_id].category != kind):
            return
        if self.item_folder(kind, real_id) == (folder_id or ""):
            return
        if kind == "source":
            if folder_id:
                self.project.source_folders[real_id] = folder_id
            else:
                self.project.source_folders.pop(real_id, None)
        else:
            table = self.project.scenes if kind == "scene" else self.project.symbol_defs
            item = table.get(real_id)
            if item is not None:
                item.folder_id = folder_id or ""
        self.mark_dirty()

    # library: sources
    def add_source_file(self, path: str, folder_id: Optional[str] = None) -> str:
        if not self.project_dir:
            raise ValueError("Save the project before importing media.")
        os.makedirs(self.project_dir, exist_ok=True)
        orig_name = os.path.basename(path)
        # Name collisions are only checked among sources (not scenes/symbols)
        # within the same folder the new source is being imported into.
        existing = {
            name for uid, name in self.project.sources.items()
            if (self.project.source_folders.get(uid) or "") == (folder_id or "")
        }
        orig_name = self._unique_name(orig_name, existing)
        ext = os.path.splitext(orig_name)[1].lower()
        uuid_ext = self._fresh_id() + ext
        assert uuid_ext not in self.project.sources, (
            f"generated source id {uuid_ext!r} unexpectedly collides with an existing source"
        )
        dest = os.path.join(self.project_dir, uuid_ext)
        shutil.copyfile(path, dest)
        self.project.sources[uuid_ext] = orig_name
        self.project.source_created[uuid_ext] = now_ms()
        if folder_id:
            self.project.source_folders[uuid_ext] = folder_id
        self.mark_dirty()
        return uuid_ext

    def add_source_file_as_symbol(self, path: str) -> tuple:
        uuid_ext = self.add_source_file(path)
        symbol_id = self.wrap_source_as_symbol(uuid_ext)
        return uuid_ext, symbol_id

    def wrap_source_as_symbol(self, uuid_ext: str) -> str:
        if uuid_ext not in self.project.sources:
            raise ValueError("Unknown source.")
        base_name = os.path.splitext(self.project.sources[uuid_ext])[0] or "Symbol"
        self.create_class(base_name)
        width = height = 0.0
        try:
            from .imaging import load_pixmap
            pix = load_pixmap(os.path.join(self.project_dir, uuid_ext)) if self.project_dir else None
            if pix is not None and not pix.isNull():
                width, height = float(pix.width()), float(pix.height())
        except Exception:
            pass
        return self.insert_new_symbol({
            "name": base_name,
            "elem_class": base_name,
            "elem_id": base_name,
            "active_image": uuid_ext,
            "inactive_image": uuid_ext,
            "width": width,
            "height": height,
        })

    def rename_source(self, uuid_ext: str, name: str):
        if uuid_ext in self.project.sources and name:
            # Name collisions are only checked among sources in the same folder.
            my_folder = self.project.source_folders.get(uuid_ext) or ""
            existing = {
                n for uid, n in self.project.sources.items()
                if uid != uuid_ext and (self.project.source_folders.get(uid) or "") == my_folder
            }
            if name != self.project.sources[uuid_ext]:
                name = self._unique_name(name, existing)
            self.project.sources[uuid_ext] = name
            self.mark_dirty()

    def replace_source_file(self, uuid_ext: str, path: str,
                             on_done: Optional[Callable[[Optional[Exception]], None]] = None) -> None:

        if uuid_ext not in self.project.sources or not self.project_dir:
            raise ValueError("Nothing to replace, or project not saved yet.")
        dest = os.path.join(self.project_dir, uuid_ext)

        def work():
            if self.invalidate_source_cache is not None:
                try:
                    self.invalidate_source_cache(uuid_ext)
                except Exception:
                    pass
            err: Optional[Exception] = None
            try:
                shutil.copyfile(path, dest)
            except OSError as e:
                err = e

            def finish():
                if err is None:
                    self.mark_dirty()
                if on_done is not None:
                    on_done(err)

            QTimer.singleShot(0, finish)

        threading.Thread(target=work, name="czeditor-replace-source", daemon=True).start()

    # scene / symbol export & import (.czescn / .czesym)

    def _collect_scene_closure(self, scene_id: str):
        scenes_out: dict = {}
        symbols_out: dict = {}
        sources_out: set = set()
        visited: set = set()

        def walk(sid: str):
            if sid in visited:
                return
            visited.add(sid)
            scene = self.project.scenes.get(sid)
            if not scene:
                return
            scenes_out[sid] = scene
            bg = scene.background or ""
            if bg.startswith("img:") and bg[4:]:
                sources_out.add(bg[4:])
            if scene.inactive_image:
                sources_out.add(scene.inactive_image)
            if scene.inactive_ref:
                walk(scene.inactive_ref)
            for p in scene.placements:
                if p.kind == "scene" and p.ref:
                    walk(p.ref)
                elif p.kind == "symbol" and p.ref:
                    edef = self.project.symbol_defs.get(p.ref)
                    if edef and p.ref not in symbols_out:
                        symbols_out[p.ref] = edef
                        if edef.active_image:
                            sources_out.add(edef.active_image)
                        if edef.inactive_image:
                            sources_out.add(edef.inactive_image)
                    for u in (p.media_list or []):
                        if u:
                            sources_out.add(u)
                elif p.kind in ("image", "video", "sound") and p.ref:
                    sources_out.add(p.ref)

        walk(scene_id)
        return scenes_out, symbols_out, sources_out

    def _write_bundle(self, dest_path: str, manifest: dict, source_uuids) -> None:
        if not self.project_dir:
            raise ValueError("Save the project before exporting.")
        tmp = dest_path + ".tmp"
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(manifest, indent=2))
                for u in source_uuids:
                    src = os.path.join(self.project_dir, u)
                    if os.path.isfile(src):
                        zf.write(src, arcname=f"assets/{u}")
            os.replace(tmp, dest_path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def export_scene(self, scene_id: str, dest_path: str) -> None:
        if scene_id not in self.project.scenes:
            raise ValueError("Unknown scene.")
        scenes, symbols, sources = self._collect_scene_closure(scene_id)
        classes = sorted({s.elem_class for s in symbols.values() if s.elem_class})
        manifest = {
            "format": "czescn",
            "app": "czeditor",
            "version": 1,
            "root_scene": scene_id,
            "scenes": {sid: sc.to_dict() for sid, sc in scenes.items()},
            "symbol_defs": {eid: sd.to_dict() for eid, sd in symbols.items()},
            "classes": classes,
            "sources": {u: self.project.sources.get(u, u) for u in sources},
        }
        self._write_bundle(dest_path, manifest, sources)

    def export_symbol(self, symbol_id: str, dest_path: str) -> None:
        edef = self.project.symbol_defs.get(symbol_id)
        if not edef:
            raise ValueError("Unknown symbol.")
        sources = {u for u in (edef.active_image, edef.inactive_image) if u}
        manifest = {
            "format": "czesym",
            "app": "czeditor",
            "version": 1,
            "symbol_id": symbol_id,
            "symbol_defs": {symbol_id: edef.to_dict()},
            "classes": [edef.elem_class] if edef.elem_class else [],
            "sources": {u: self.project.sources.get(u, u) for u in sources},
        }
        self._write_bundle(dest_path, manifest, sources)

    def _read_bundle(self, zip_path: str):
        try:
            zf = zipfile.ZipFile(zip_path, "r")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (KeyError, OSError, ValueError, zipfile.BadZipFile) as e:
            raise ValueError(f"Not a valid czeditor bundle: {e}")
        return manifest, zf

    def _extract_bundle_sources(self, manifest: dict, zf: zipfile.ZipFile) -> dict:
        if not self.project_dir:
            raise ValueError("Save the project before importing.")
        os.makedirs(self.project_dir, exist_ok=True)
        existing_names = set(self.project.sources.values())
        old_to_new: dict = {}
        for u, orig_name in (manifest.get("sources") or {}).items():
            arcname = f"assets/{u}"
            if arcname not in zf.namelist():
                continue
            ext = os.path.splitext(u)[1]
            new_u = new_uuid() + ext
            dest = os.path.join(self.project_dir, new_u)
            with zf.open(arcname) as fin, open(dest, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            name = self._unique_name(orig_name or new_u, existing_names)
            existing_names.add(name)
            self.project.sources[new_u] = name
            self.project.source_created[new_u] = now_ms()
            old_to_new[u] = new_u
        return old_to_new

    def _merge_scene_bundle(self, manifest: dict, zf: zipfile.ZipFile) -> str:
        old_to_new_source = self._extract_bundle_sources(manifest, zf)

        old_to_new_symbol: dict = {}
        for sid, sd in (manifest.get("symbol_defs") or {}).items():
            new_id = new_uuid()
            sdd = dict(sd)
            sdd["id"] = new_id
            existing = {s.name for s in self.project.symbol_defs.values()}
            sdd["name"] = self._unique_name(sdd.get("name") or "Symbol", existing)
            if sdd.get("active_image"):
                sdd["active_image"] = old_to_new_source.get(sdd["active_image"], sdd["active_image"])
            if sdd.get("inactive_image"):
                sdd["inactive_image"] = old_to_new_source.get(sdd["inactive_image"], sdd["inactive_image"])
            sdd["folder_id"] = ""
            self.project.symbol_defs[new_id] = SymbolDef.from_dict(sdd)
            old_to_new_symbol[sid] = new_id

        old_to_new_scene: dict = {sid: new_uuid() for sid in (manifest.get("scenes") or {})}
        existing_scene_names = {s.name for s in self.project.scenes.values()}
        for sid, sd in (manifest.get("scenes") or {}).items():
            new_id = old_to_new_scene[sid]
            scd = dict(sd)
            scd["id"] = new_id
            scd["name"] = self._unique_name(scd.get("name") or "Scene", existing_scene_names)
            existing_scene_names.add(scd["name"])
            scd["folder_id"] = ""
            bg = scd.get("background") or ""
            if bg.startswith("img:"):
                u = bg[4:]
                scd["background"] = "img:" + old_to_new_source.get(u, u)
            if scd.get("inactive_image"):
                scd["inactive_image"] = old_to_new_source.get(scd["inactive_image"], scd["inactive_image"])
            if scd.get("inactive_ref") in old_to_new_scene:
                scd["inactive_ref"] = old_to_new_scene[scd["inactive_ref"]]
            new_placements = []
            for pd in scd.get("placements") or []:
                pdd = dict(pd)
                pdd["id"] = new_uuid()
                kind = pdd.get("kind")
                if kind == "scene" and pdd.get("ref") in old_to_new_scene:
                    pdd["ref"] = old_to_new_scene[pdd["ref"]]
                elif kind == "symbol":
                    if pdd.get("ref") in old_to_new_symbol:
                        pdd["ref"] = old_to_new_symbol[pdd["ref"]]
                    pdd["media_list"] = [old_to_new_source.get(u, u) for u in (pdd.get("media_list") or [])]
                elif kind in ("image", "video", "sound") and pdd.get("ref") in old_to_new_source:
                    pdd["ref"] = old_to_new_source[pdd["ref"]]
                new_placements.append(pdd)
            scd["placements"] = new_placements
            self.project.scenes[new_id] = Scene.from_dict(scd)

        for cls in manifest.get("classes") or []:
            self.project.ensure_class(cls)
        self.mark_dirty()
        return old_to_new_scene.get(manifest.get("root_scene"), "")

    def _merge_symbol_bundle(self, manifest: dict, zf: zipfile.ZipFile) -> str:
        old_to_new_source = self._extract_bundle_sources(manifest, zf)
        sid = manifest.get("symbol_id")
        sd = (manifest.get("symbol_defs") or {}).get(sid)
        if not sd:
            raise ValueError("Symbol bundle has no symbol data.")
        new_id = new_uuid()
        sdd = dict(sd)
        sdd["id"] = new_id
        existing = {s.name for s in self.project.symbol_defs.values()}
        sdd["name"] = self._unique_name(sdd.get("name") or "Symbol", existing)
        if sdd.get("active_image"):
            sdd["active_image"] = old_to_new_source.get(sdd["active_image"], sdd["active_image"])
        if sdd.get("inactive_image"):
            sdd["inactive_image"] = old_to_new_source.get(sdd["inactive_image"], sdd["inactive_image"])
        sdd["folder_id"] = ""
        self.project.symbol_defs[new_id] = SymbolDef.from_dict(sdd)
        for cls in manifest.get("classes") or []:
            self.project.ensure_class(cls)
        if sdd.get("elem_class"):
            self.project.ensure_class(sdd["elem_class"])
        self.mark_dirty()
        return new_id

    def import_scene(self, zip_path: str) -> str:
        manifest, zf = self._read_bundle(zip_path)
        try:
            if manifest.get("format") != "czescn":
                raise ValueError("That file isn't a scene bundle (.czescn).")
            return self._merge_scene_bundle(manifest, zf)
        finally:
            zf.close()

    def import_symbol(self, zip_path: str) -> str:
        manifest, zf = self._read_bundle(zip_path)
        try:
            if manifest.get("format") != "czesym":
                raise ValueError("That file isn't a symbol bundle (.czesym).")
            return self._merge_symbol_bundle(manifest, zf)
        finally:
            zf.close()

