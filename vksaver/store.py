# -*- coding: utf-8 -*-
"""Состояние «области» — папки, где лежат HTML и рядом подпапки с медиа.

Файлы состояния:
  <папка>/photos/_index.json, <папка>/voice/_index.json  — url -> имя файла
  <папка>/_media_failed.json  — url -> ошибка (окончательно недоступные)
  <папка>/_media_retry.json   — url -> число запусков подряд с временной ошибкой
"""
import json
import os
from pathlib import Path

from .common import KINDS

INDEX_NAME = "_index.json"
FAILED_NAME = "_media_failed.json"
RETRY_NAME = "_media_retry.json"


def load_json(p):
    p = Path(p)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(tmp, p)


class ScopeState:
    def __init__(self, scope_dir):
        self.dir = Path(scope_dir)
        self._index = None                  # читается при первом обращении
        self.failed = load_json(self.dir / FAILED_NAME)
        self.retry = load_json(self.dir / RETRY_NAME)

    @property
    def index(self):
        if self._index is None:
            self._index = {k: load_json(self.dir / m["folder"] / INDEX_NAME)
                           for k, m in KINDS.items() if m["folder"]}
        return self._index

    def folder(self, kind):
        return self.dir / KINDS[kind]["folder"]

    def local_name(self, url, kind):
        """Имя скачанного файла или None."""
        n = self.index.get(kind, {}).get(url)
        if n and (self.folder(kind) / n).exists():
            return n
        return None

    def save(self):
        for k, idx in (self._index or {}).items():
            if idx:
                save_json(self.folder(k) / INDEX_NAME, idx)
        for name, obj in ((FAILED_NAME, self.failed), (RETRY_NAME, self.retry)):
            p = self.dir / name
            if obj:
                save_json(p, obj)
            elif p.exists():
                try:
                    p.unlink()
                except OSError:
                    save_json(p, {})
