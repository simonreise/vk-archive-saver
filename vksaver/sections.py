# -*- coding: utf-8 -*-
"""Разделы архива VK: какие HTML-файлы к ним относятся и что в них можно скачивать."""
from pathlib import Path

ALL = "all"


class Section:
    def __init__(self, sid, title, patterns, note="", kinds=("photo", "doc", "video"),
                 default_off=("video",)):
        self.id = sid
        self.title = title
        self.patterns = patterns     # glob-шаблоны относительно корня архива
        self.note = note
        self.kinds = list(kinds)     # какие виды файлов в разделе вообще встречаются
        self.default_off = set(default_off)

    def html_files(self, root):
        root = Path(root)
        seen = set()
        out = []
        for pat in self.patterns:
            for p in root.glob(pat):
                if p.is_file() and p not in seen:
                    seen.add(p)
                    out.append(p)
        return sorted(out)


SECTIONS = [
    Section("messages", "Сообщения", ["messages/*/messages*.html"],
            "Переписки: фото, голосовые, документы, видео из вложений.",
            kinds=("photo", "voice", "doc", "video")),
    Section("wall", "Стена", ["wall/wall*.html", "wall/*/comments*.html"],
            "Записи и комментарии на стене (включая репосты)."),
    Section("profile", "Профиль",
            ["photos/**/*.html", "profile/*.html", "video/**/*.html", "audio/**/*.html"],
            "Фотоальбомы и комментарии к фото, документы профиля, аватар, видеоальбомы."),
    Section("likes", "Лайки и закладки", ["likes/**/*.html", "bookmarks/**/*.html"],
            "Чужие фото, видео и записи, которым вы ставили «Нравится» или добавили в закладки."),
]
SECTIONS_BY_ID = {s.id: s for s in SECTIONS}
# названия разделов из ранних сборок -> новые
SECTION_ALIASES = {"photos": ["profile"], "other": ["profile", "likes"]}


# --- категории переписок ------------------------------------------------------
CHAT_CATEGORIES = [("personal", "Личные"), ("group", "Беседы"), ("community", "Сообщества")]


def scope_category(sid, scope_dir):
    """Категория папки: для сообщений — личные/беседы/сообщества, иначе 'all'."""
    if sid != "messages":
        return ALL
    name = scope_dir.rsplit("/", 1)[-1]
    try:
        peer = int(name)
    except ValueError:
        return "personal"
    if peer >= 2000000000:
        return "group"
    if peer < 0:
        return "community"
    return "personal"


def categories(sid):
    return CHAT_CATEGORIES if sid == "messages" else [(ALL, "")]
