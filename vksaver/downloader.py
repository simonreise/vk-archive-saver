# -*- coding: utf-8 -*-
"""Загрузка файла потоком на диск с проверкой, что пришло именно то, что ожидалось."""
import hashlib
import http.client
import os
import re
import socket
import time
import uuid
import mimetypes
import urllib.error
import urllib.request
from urllib.parse import urlsplit, unquote

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
EXT_BY_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
               "image/webp": ".webp", "image/heic": ".heic",
               "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3",
               "audio/mp4": ".m4a", "video/mp4": ".mp4", "video/webm": ".webm"}
DEFAULT_EXT = {"photo": ".jpg", "voice": ".ogg", "video": ".mp4", "doc": ""}
PERMANENT_HTTP = (403, 404, 410, 424)   # повтор не поможет
TRIES = 5
RETRY_DELAY = 5          # базовая пауза между повторами, с (растёт с каждой попыткой)
CHUNK = 1 << 20


class PermanentError(Exception):
    """Ссылка мертва окончательно — повторять бессмысленно."""


class Stopped(Exception):
    pass


def looks_like(kind, data, ctype):
    if kind == "voice":
        return (data[:4] == b"OggS" or data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3")
                or data[4:8] == b"ftyp" or ctype.startswith("audio/"))
    if kind == "photo":
        return (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
                or data[:4] == b"GIF8" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                or data[4:12] in (b"ftypheic", b"ftypmif1", b"ftypheix"))
    if kind == "video":
        return (data[4:8] == b"ftyp" or data[:4] == b"\x1a\x45\xdf\xa3" or ctype.startswith("video/")
                or ctype == "application/octet-stream")
    # документ: что угодно, кроме HTML-страницы (страница входа/ошибки)
    return not is_html(data, ctype)


def is_html(data, ctype):
    head = data[:512].lstrip().lower()
    return ctype in ("text/html", "application/xhtml+xml") or head.startswith((b"<!doctype html", b"<html"))


def http_error(e):
    """HTTPError -> PermanentError (если повтор бесполезен) или None."""
    if isinstance(e, urllib.error.HTTPError) and e.code in PERMANENT_HTTP:
        return PermanentError(f"HTTP {e.code} {e.reason}")
    return None


def retrying(func, stop=None, tries=TRIES):
    """Вызывает func() с повторами при сетевых ошибках и 5xx/429."""
    last = None
    for attempt in range(tries):
        if stop is not None and stop.is_set():
            raise Stopped()
        try:
            return func()
        except urllib.error.HTTPError as e:
            pe = http_error(e)
            if pe:
                raise pe
            last = f"HTTP {e.code} {e.reason}"
            delay = 12 * RETRY_DELAY if e.code == 429 else RETRY_DELAY * (attempt + 1)
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError,
                http.client.HTTPException) as e:
            last = str(getattr(e, "reason", e))
            delay = RETRY_DELAY * (attempt + 1)
        if stop is None:
            time.sleep(delay)
        elif stop.wait(delay):
            raise Stopped()
    raise RuntimeError(last)


def open_url(url, opener=None, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://vk.ru/"})
    return (opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout))


def server_filename(resp):
    disp = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", disp) or re.search(r'filename="?([^";]+)', disp)
    if not m:
        return None
    name = unquote(m.group(1))
    try:                                           # VK иногда отдаёт UTF-8 как latin-1
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name


def stream_to_temp(resp, kind, target_dir, stop=None, head=None):
    """Пишет ответ во временный файл. -> (tmp_path, ctype, size).

    head — уже прочитанное начало ответа (если было).
    """
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    first = head if head is not None else resp.read(65536)
    if not looks_like(kind, first, ctype):
        resp.close()
        raise PermanentError(f"неожиданный ответ ({ctype or '?'}, начало {first[:16]!r})")
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_dir / f"_dl_{uuid.uuid4().hex}.part"     # уникально для каждого потока
    size = 0
    try:
        with open(tmp, "wb") as f:
            f.write(first)
            size += len(first)
            while True:
                if stop is not None and stop.is_set():
                    raise Stopped()
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    finally:
        resp.close()
    return tmp, ctype, size


def clean_name(name):
    name = "".join(c if (c.isalnum() or c in ".-_ ()[],") and _cp1251_ok(c) else "_" for c in name)
    return re.sub(r"[\s_]+", lambda m: m.group(0)[0], name).strip(" ._")


def pick_name(url, used, kind, hint=None, ctype=""):
    """Имя файла: подсказка (имя с сервера/из архива) или имя из URL; уникальное в папке."""
    name = clean_name(hint or "") or clean_name(os.path.basename(urlsplit(url).path)) or "file"
    stem, ext = os.path.splitext(name)
    real_ext = EXT_BY_TYPE.get(ctype)
    if real_ext and ext.lower() not in (real_ext, ".jpeg" if real_ext == ".jpg" else real_ext):
        if kind != "doc" or not ext:
            ext = real_ext
    if not ext and kind == "doc" and ctype and ctype not in ("application/octet-stream", "binary/octet-stream"):
        ext = mimetypes.guess_extension(ctype) or ""
    if not ext:
        ext = DEFAULT_EXT.get(kind, "")
    stem = stem[:80] or "file"
    name = stem + ext
    n = 1
    while name.lower() in used:
        suffix = hashlib.sha1(url.encode()).hexdigest()[:8] if n == 1 else f"{n}"
        name = f"{stem}_{suffix}{ext}"
        n += 1
    return name


def _cp1251_ok(c):
    try:
        c.encode("cp1251")
        return True
    except UnicodeEncodeError:
        return False
