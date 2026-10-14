# -*- coding: utf-8 -*-
"""Настройки программы: settings.json рядом с папкой проекта."""
from pathlib import Path

from .store import load_json, save_json

PROJECT_DIR = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_DIR / "settings.json"
DEFAULTS = {"archive": "", "workers": 6}


def load_settings():
    s = dict(DEFAULTS)
    s.update(load_json(SETTINGS_PATH))
    if not s["archive"]:
        s["archive"] = find_archive() or ""
    _migrate_kinds(s)
    return s


def find_archive():
    """Первый запуск: ищем выгрузку VK (index.html + messages/) рядом с папкой проекта
    или внутри неё. Предпочитаем папки, названные archive*/Archive*."""
    cands = []
    for base in (PROJECT_DIR.parent, PROJECT_DIR):
        try:
            cands += [p for p in base.iterdir() if p.is_dir() and p != PROJECT_DIR]
        except OSError:
            pass
    cands.sort(key=lambda p: (not p.name.lower().startswith("archive"), p.name.lower()))
    for cand in cands:
        if (cand / "index.html").exists() and (cand / "messages").is_dir():
            return str(cand)
    return None


def _migrate_kinds(s):
    """Галочки вкладок «Фото»/«Прочее» из ранних сборок -> «Профиль» и «Лайки и закладки»."""
    kinds = s.get("kinds")
    if not isinstance(kinds, dict):
        return
    def flat(v):                              # {cat: {k: bool}} или {k: bool} -> {k: bool}
        if isinstance(v, dict) and v and all(isinstance(x, bool) for x in v.values()):
            return v
        if isinstance(v, dict):
            for x in v.values():
                if isinstance(x, dict):
                    return x
        return None
    other = kinds.pop("other", None)
    photos = kinds.pop("photos", None)
    if "profile" not in kinds:
        src = (other or {}).get("own") if isinstance(other, dict) and "own" in other else None
        src = src or flat(photos) or flat(other)
        if src:
            kinds["profile"] = {"all": dict(src)}
    if "likes" not in kinds and isinstance(other, dict) and isinstance(other.get("likes"), dict):
        kinds["likes"] = {"all": dict(other["likes"])}


def save_settings(s):
    save_json(SETTINGS_PATH, s)
