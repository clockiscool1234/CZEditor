import os

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".aiff", ".aif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".wmv", ".flv", ".ogv", ".3gp"}


def is_audio(filename: str) -> bool:
    return os.path.splitext((filename or "").lower())[1] in AUDIO_EXTS


def is_video(filename: str) -> bool:
    return os.path.splitext((filename or "").lower())[1] in VIDEO_EXTS
