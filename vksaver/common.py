# -*- coding: utf-8 -*-
"""Общие константы, виды медиа и классификация ссылок."""
import os
import re
from urllib.parse import urlsplit, parse_qs

HTML_ENCODING = "cp1251"          # кодировка страниц выгрузки VK
WORK_DIR = "_vksaver"              # служебная папка внутри архива (кэш, журналы)
BACKUP_DIR = "_backup_html"        # копии исходных HTML (внутри архива)

# --- виды медиа -------------------------------------------------------------
# folder  — подпапка рядом с HTML, куда кладутся файлы этого вида
# target  — для ссылок на страницы VK: какой файл из них получается
#           (такие ссылки скачиваются только со входом в VK по cookie)
KINDS = {
    "photo":      {"title": "Фото",      "folder": "photos"},
    "voice":      {"title": "Голосовые", "folder": "voice"},
    "doc":        {"title": "Документы", "folder": "docs"},
    "video":      {"title": "Видео",     "folder": "video"},
    "photo_page": {"title": "Фото (страница VK)",      "folder": None, "target": "photo"},
    "doc_page":   {"title": "Документы (страница VK)", "folder": None, "target": "doc"},
    "video_page": {"title": "Видео (страница VK)",     "folder": None, "target": "video"},
}
FILE_KINDS = ["photo", "voice", "doc", "video"]          # строки таблицы и галочки
KIND_ORDER = FILE_KINDS
FOLDER_TO_KIND = {v["folder"]: k for k, v in KINDS.items() if v["folder"]}


def target(kind):
    """Вид файла, который получится из ссылки этого вида."""
    return KINDS[kind].get("target", kind)


def needs_login(kind):
    return "target" in KINDS[kind]


VK_FILE_HOSTS = re.compile(r"(^|\.)(userapi\.com|vkuserphoto\.ru|vk-cdn\.net|vkuser\.net|vkuseraudio\.net)$", re.I)
VK_SITE_HOSTS = re.compile(r"^(m\.)?vk\.(ru|com)$", re.I)
AUDIO_EXT = (".ogg", ".opus", ".mp3", ".m4a", ".oga")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp")


def classify(url, plain_href=False, photo_pages=False):
    """Вид медиа для ссылки или None, если ссылка нас не интересует.

    plain_href=True — обычная ссылка (не вложение): из страничных ссылок VK
    учитываем документы и видео, а фото — только при photo_pages=True (лайки,
    закладки); в фотоальбомах такие ссылки дублируют картинку.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return None
    path = parts.path.lower()
    if VK_FILE_HOSTS.search(host):
        if plain_href:
            return None
        if "/amsg" in path or path.endswith(AUDIO_EXT):
            return "voice"
        ext = os.path.splitext(path)[1]
        if "/doc" in path or (ext and ext not in IMAGE_EXT):
            return "doc"
        return "photo"
    if VK_SITE_HOSTS.match(host):
        if path.startswith("/doc"):
            return "doc_page"
        if path.startswith("/video"):
            return "video_page"
        if plain_href and not photo_pages:
            return None
        if path.startswith("/photo"):
            return "photo_page"
        if path.startswith("/im") and "photo" in (parse_qs(parts.query).get("z", [""])[0]):
            return "photo_page"
    return None


def local_kind(value):
    """Вид медиа для локальной (уже скачанной) ссылки вида photos/x.jpg."""
    head = value.split("/", 1)[0]
    if "/" in value and not value.startswith(("http:", "https:", "..")) \
            and not value.lower().split("?")[0].endswith((".html", ".htm")):
        return FOLDER_TO_KIND.get(head)
    return None


def fmt_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024.0


def rel_posix(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")
