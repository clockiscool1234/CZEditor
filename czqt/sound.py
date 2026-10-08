from __future__ import annotations

import os
import sys
import time
from typing import Optional

from PyQt6.QtCore import QObject, QUrl
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

from .model import Project, Scene
STUCK_GRACE_SEC = 1.5


class _Voice:
    __slots__ = ("player", "audio_out", "created_at")

    def __init__(self, player: QMediaPlayer, audio_out: QAudioOutput):
        self.player = player
        self.audio_out = audio_out
        self.created_at = time.monotonic()


class SoundEngine(QObject):
    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._players: dict[str, _Voice] = {}

    def _destroy_player(self, player: QMediaPlayer, audio_out: QAudioOutput):
        try:
            player.stop()
        except Exception:
            pass
        try:
            player.setAudioOutput(None)
        except Exception:
            pass
        try:
            player.deleteLater()
        except Exception:
            pass
        try:
            audio_out.deleteLater()
        except Exception:
            pass

    def stop_all(self):
        for _pid, voice in list(self._players.items()):
            self._destroy_player(voice.player, voice.audio_out)
        self._players.clear()

    def _play_at_offset(self, player: QMediaPlayer, offset_ms: int):
        def start(_status=None):
            if offset_ms > 0:
                player.setPosition(offset_ms)
            player.play()

        status = player.mediaStatus()
        ready_states = (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        )
        if status in ready_states:
            start()
            return

        def on_status_changed(new_status):
            if new_status in ready_states:
                try:
                    player.mediaStatusChanged.disconnect(on_status_changed)
                except Exception:
                    pass
                start()
            elif new_status == QMediaPlayer.MediaStatus.InvalidMedia:
                try:
                    player.mediaStatusChanged.disconnect(on_status_changed)
                except Exception:
                    pass

        player.mediaStatusChanged.connect(on_status_changed)

    def _is_stuck(self, voice: _Voice) -> bool:
        try:
            if voice.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                return False
            if voice.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                return False
        except Exception:
            return False
        return (time.monotonic() - voice.created_at) > STUCK_GRACE_SEC

    @staticmethod
    def _offset_seconds(p, t: float) -> tuple[float, float]:
        speed = p.speed if p.speed and p.speed > 0 else 1.0
        elapsed_src = (t - p.start) * speed
        clip_src_dur = (p.end - p.start) * speed
        src_start = p.src_offset or 0.0
        if clip_src_dur > 0:
            if p.loop:
                elapsed_src = elapsed_src % clip_src_dur
            else:
                elapsed_src = min(elapsed_src, clip_src_dur)
        return max(0.0, src_start + elapsed_src), speed

    def _spawn_voice(self, p, project: Project, project_dir: str, offset_s: float, speed: float) -> None:
        if p.ref not in project.sources:
            return
        path = os.path.join(project_dir, p.ref)
        if not os.path.isfile(path):
            return
        try:
            player = QMediaPlayer(self)
            audio_out = QAudioOutput(self)
            volume = p.volume if p.volume is not None else 1.0
            audio_out.setVolume(max(0.0, min(1.0, volume)))
            player.setAudioOutput(audio_out)
            player.setSource(QUrl.fromLocalFile(path))
            try:
                player.setPlaybackRate(speed)
            except Exception:
                pass
            try:
                player.setLoops(QMediaPlayer.Loops.Infinite if p.loop else 1)
            except Exception:
                pass
            self._play_at_offset(player, int(offset_s * 1000))
            self._players[p.id] = _Voice(player, audio_out)
        except Exception as e:
            print(f"[sound] failed to play {p.ref!r}: {e}", file=sys.stderr)

    def seek(
        self,
        project: Optional[Project],
        project_dir: Optional[str],
        scene: Optional[Scene],
        t: float,
        playing: bool,
    ):
        if not playing or not scene or not project or not project_dir:
            if self._players:
                self.stop_all()
            return

        active = {
            p.id: p
            for p in scene.placements
            if p.kind in ("sound", "video") and not p.muted and p.start <= t <= p.end
        }

        for pid in list(self._players.keys()):
            if pid not in active:
                voice = self._players.pop(pid)
                self._destroy_player(voice.player, voice.audio_out)

        for pid, p in active.items():
            offset_s, speed = self._offset_seconds(p, t)
            voice = self._players.get(pid)
            if voice is not None:
                try:
                    voice.player.setPosition(int(offset_s * 1000))
                except Exception:
                    pass
                continue
            self._spawn_voice(p, project, project_dir, offset_s, speed)

    def update(
        self,
        project: Optional[Project],
        project_dir: Optional[str],
        scene: Optional[Scene],
        t: float,
        playing: bool,
    ):
        if not playing or not scene or not project or not project_dir:
            if self._players:
                self.stop_all()
            return

        active_ids = {
            p.id
            for p in scene.placements
            if p.kind in ("sound", "video") and not p.muted and p.start <= t <= p.end
        }

        for pid in list(self._players.keys()):
            voice = self._players[pid]
            if pid not in active_ids or self._is_stuck(voice):
                self._players.pop(pid)
                self._destroy_player(voice.player, voice.audio_out)

        for p in scene.placements:
            if p.id not in self._players or not p.loop or p.kind not in ("sound", "video"):
                continue
            clip_dur = p.end - p.start
            speed = p.speed if p.speed and p.speed > 0 else 1.0
            clip_src_dur = clip_dur * speed
            if clip_src_dur <= 0:
                continue
            elapsed_src = (t - p.start) * speed
            pos_in_clip = elapsed_src % clip_src_dur
            voice = self._players[p.id]
            try:
                cur_pos_ms = voice.player.position()
            except Exception:
                continue
            expected_ms = int((p.src_offset or 0.0) * 1000 + pos_in_clip * 1000)
            clip_end_ms = int(((p.src_offset or 0.0) + clip_src_dur) * 1000)
            if cur_pos_ms > clip_end_ms + 500:
                self._players.pop(p.id)
                self._destroy_player(voice.player, voice.audio_out)

        for p in scene.placements:
            if p.kind not in ("sound", "video") or p.id not in active_ids or p.id in self._players:
                continue
            offset_s, speed = self._offset_seconds(p, t)
            self._spawn_voice(p, project, project_dir, offset_s, speed)
