# -*- coding: utf-8 -*-
"""Превращение ссылки на страницу VK (vk.ru/doc…, /photo…, /video…) в файл.

Работает только с cookie вошедшего пользователя (VKSession). Структура страниц
VK меняется, поэтому каждый способ — эвристика; неразобранные страницы
сохраняются в <архив>/_vksaver/debug/ для анализа.
"""
import html as htmlmod
import json
import re
import threading
import time
import urllib.parse
from urllib.parse import urlsplit, parse_qs

from .downloader import PermanentError, is_html
from .vkauth import LoginRequired, CaptchaRequired, is_login_page, is_captcha_page, VK_BASE

TARGET = {"photo_page": "photo", "doc_page": "doc", "video_page": "video"}

DENIED_RE = re.compile(
    r"Ошибка доступа|Доступ запрещ|Access denied|был[аио]? удал[её]н|has been deleted|"
    r"Видеозапись (была )?изъята|Видео недоступно|Video is unavailable|"
    r"Документ не найден|Файл не найден|Фотография не найдена|Photo not found|"
    r"недоступен или удал|Доступ ограничен|Access denied|"
    r"This video is not available|private video|Это видео доступно только", re.I)
EXTERNAL_VIDEO_RE = re.compile(r"(youtube\.com/embed|youtube-nocookie|player\.vimeo|rutube\.ru/play|"
                               r"coub\.com/embed|ok\.ru/videoembed)", re.I)


class ParseError(Exception):
    """Страница получена, но найти на ней файл не удалось (сохраняется в debug)."""


class Resolved:
    """Результат: либо прямой URL, либо уже открытый ответ с самим файлом."""
    def __init__(self, url=None, resp=None, head=None, name=None, note=""):
        self.url, self.resp, self.head, self.name, self.note = url, resp, head, name, note


# ------------------------------------------------------------------ утилиты
def norm(url):
    """vk.com -> vk.ru (cookie у нас для vk.ru), m.vk -> vk."""
    p = urlsplit(url)
    host = re.sub(r"^(m\.)?vk\.com$", "vk.ru", p.hostname or "")
    host = re.sub(r"^m\.vk\.ru$", "vk.ru", host)
    return urllib.parse.urlunsplit(("https", host, p.path, p.query, ""))


def unescape_js(s):
    return htmlmod.unescape(s.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "="))


def visible_text(text):
    """Текст, который видит человек: без скриптов, стилей, комментариев и тегов."""
    t = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1\s*>", " ", text)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmlmod.unescape(t))


def denied(text, debug, kind, url, what):
    """Если на видимой части страницы есть сообщение об ошибке — PermanentError."""
    vis = visible_text(text)
    m = DENIED_RE.search(vis)
    if m:
        snip = vis[max(0, m.start() - 40): m.end() + 40].strip()
        debug.save(kind, url, text, f"признано недоступным: {snip}")
        raise PermanentError(f"{what} недоступно или удалено («…{snip}…»)")


def find_urls(text, host_re):
    """Все абсолютные URL с подходящим хостом (в HTML и в JSON внутри страниц)."""
    t = unescape_js(text)
    out = []
    for m in re.finditer(r"https?://([a-z0-9.-]+)(/[^\s\"'<>\\)]*)", t, re.I):
        if re.search(host_re, m.group(1), re.I):
            out.append(m.group(0))
    return list(dict.fromkeys(out))


class DebugSaver:
    """Сохраняет до N неразобранных страниц каждого вида за запуск."""
    def __init__(self, folder, limit=5):
        self.folder, self.limit, self.count = folder, limit, {}
        self.lock = threading.Lock()

    def save(self, kind, url, text, reason):
        with self.lock:
            n = self.count.get(kind, 0)
            if self.folder is None or n >= self.limit:
                return None
            self.count[kind] = n + 1
            self.folder.mkdir(parents=True, exist_ok=True)
            p = self.folder / f"{kind}_{time.strftime('%Y%m%d_%H%M%S')}_{n + 1}.html"
        from .vkapi import redact
        p.write_text(f"<!-- vksaver debug\n url: {url}\n reason: {reason}\n-->\n" + redact(text),
                     encoding="utf-8", errors="replace")
        return p


# ------------------------------------------------------------------ запрос страницы
def _open_page(session, url, data=None, xhr=False):
    """-> (final_url, ctype, resp, head_bytes). resp не прочитан дальше head."""
    import urllib.request
    from .vkauth import USER_AGENT
    session.throttle()
    headers = {"User-Agent": USER_AGENT, "Referer": VK_BASE + "/",
               "Accept-Language": "ru-RU,ru;q=0.9"}
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    resp = session.opener.open(req, timeout=40)
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    head = resp.read(65536)
    return resp.geturl(), ctype, resp, head


def _read_text(resp, head):
    try:
        rest = resp.read()
    finally:
        resp.close()
    raw = head + rest
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")


def _check_page(final, text):
    if is_captcha_page(text):
        raise CaptchaRequired("VK просит пройти капчу. Сделайте паузу (час-другой) и продолжите.")
    if is_login_page(final, text):
        raise LoginRequired("VK показывает страницу входа — cookie устарели. Загрузите свежие.")


# ------------------------------------------------------------------ документы
def resolve_doc(session, url, debug, label=None):
    final, ctype, resp, head = _open_page(session, norm(url))
    if not is_html(head, ctype):                     # VK сразу отдал файл
        return Resolved(resp=resp, head=head, name=label, note="файл по ссылке")
    text = _read_text(resp, head)
    _check_page(final, text)
    cands = [unescape_js(m.group(1)) for m in re.finditer(r'"docUrl"\s*:\s*"(https?:[^"]+)"', text)]
    cands += [u for u in find_urls(text, r"^psv\d+\.(userapi\.com|vkuserphoto\.ru)$|^vkuserdoc|"
                                         r"userapi\.com$|vkuserphoto\.ru$")
              if re.search(r"/(doc|docs|c\d+)/|\?extra=|/s/v1/d\d*/", u)]
    t = re.search(r'"docTitle"\s*:\s*"([^"]+)"', text)
    if t and not label:
        label = unescape_js(t.group(1))
    m = re.search(r"/doc(-?\d+_\d+)", url)
    if m:                                             # кнопка «Скачать»: vk.ru/docX_Y?hash=…&dl=…
        cands += [u for u in find_urls(text, r"^(m\.)?vk\.(ru|com)$")
                  if f"/doc{m.group(1)}?" in u and ("dl=" in u or "hash=" in u)]
    if cands:
        return Resolved(url=cands[0], name=label, note="ссылка со страницы документа")
    denied(text, debug, "doc", url, "документ")
    debug.save("doc", url, text, "не найдена ссылка на файл")
    raise ParseError("не удалось найти файл на странице документа")


# ------------------------------------------------------------------ фото
def _photo_id(url):
    p = urlsplit(url)
    m = re.search(r"photo(-?\d+_\d+)", p.path)
    if m:
        return m.group(1), None
    z = parse_qs(p.query).get("z", [""])[0]
    m = re.search(r"photo(-?\d+_\d+)(?:/(\w+))?", urllib.parse.unquote(z))
    if m:
        return m.group(1), m.group(2)
    return None, None


def _photo_area(u):
    m = re.search(r"[?&]size=(\d+)x(\d+)", u)
    if m:
        return int(m.group(1)) * int(m.group(2))
    m = re.search(r"/([wzyxrqpmso])_[0-9a-f]+\.jpg", u)      # старые ссылки: буква = размер
    return {"w": 9, "z": 8, "y": 7, "x": 6, "r": 5, "q": 4, "p": 3, "o": 2, "m": 1, "s": 0}.get(
        m.group(1), 0) if m else 0


def resolve_photo(session, url, debug, label=None):
    pid, lst = _photo_id(url)
    if not pid:
        raise PermanentError("не удалось понять ID фото из ссылки")
    pages = [f"{VK_BASE}/photo{pid}" + (f"?list={lst}" if lst else ""), f"{VK_BASE}/photo{pid}"]
    text = ""
    for page in dict.fromkeys(pages):
        final, ctype, resp, head = _open_page(session, page)
        if not is_html(head, ctype):
            return Resolved(resp=resp, head=head)
        text = _read_text(resp, head)
        _check_page(final, text)
        pf = re.search(r'"method":"photos\.getById"(?:(?!"method").)*?"error":\{"error_code":(\d+),'
                       r'"error_msg":"([^"]*)"', text)
        if pf:
            raise PermanentError(f"нет доступа к фото (VK: {pf.group(2)})")
        urls = find_urls(text, r"(^sun\d+(-\d+)?\.userapi\.com$|vkuserphoto\.ru$|^pp\.userapi\.com$)")
        # аватарки и превью приходят с crop=… или маленьким size=…; служебные картинки — /images/
        urls = [u for u in urls if not re.search(r"/(images|emoji|stickers?)/", u)
                and "crop=" not in u]
        if urls:
            # берём ссылки рядом с упоминанием ID фото, иначе — все
            pos = text.find(pid)
            near = []
            if pos >= 0:
                window = unescape_js(text[max(0, pos - 2000): pos + 30000])
                near = [u for u in urls if u in window]
            best = max(near or urls, key=_photo_area)
            return Resolved(url=best, note=f"фото со страницы ({len(urls)} вариантов)")
    denied(text, debug, "photo", url, "фото")
    debug.save("photo", url, text, "не найдено изображение")
    raise ParseError("не удалось найти изображение на странице фото")


# ------------------------------------------------------------------ видео
def _video_id(url):
    m = re.search(r"video(-?\d+_\d+)", urllib.parse.unquote(url))
    return m.group(1) if m else None


def _pick_video(text, max_q):
    t = unescape_js(text)
    found = {}
    for m in re.finditer(r'"(?:url|cache)(\d{3,4})"\s*:\s*"(https?://[^"]+)"', t):
        found.setdefault(int(m.group(1)), m.group(2))
    for m in re.finditer(r'<source[^>]+src="(https?://[^"]+)"[^>]*>', t):      # мобильная версия
        q = re.search(r"(\d{3,4})p?\b", m.group(0))
        found.setdefault(int(q.group(1)) if q else 0, htmlmod.unescape(m.group(1)))
    if not found:
        return None, None
    ok = [q for q in found if q <= max_q] or [min(found)]
    q = max(ok)
    return found[q], q


def resolve_video(session, url, debug, label=None, max_q=720):
    vid = _video_id(url)
    if not vid:
        raise PermanentError("не удалось понять ID видео из ссылки")
    texts = []
    # 1) al_video.php — так плеер VK получает ссылки на файлы
    try:
        final, ctype, resp, head = _open_page(session, f"{VK_BASE}/al_video.php?act=show",
                                              data={"act": "show", "al": "1", "video": vid}, xhr=True)
        text = _read_text(resp, head)
        _check_page(final, text)
        texts.append(("al_video", text))
        u, q = _pick_video(text, max_q)
        if u:
            return Resolved(url=u, name=f"video{vid}_{q}p.mp4", note=f"{q}p (al_video)")
    except (LoginRequired, CaptchaRequired, PermanentError):
        raise
    except Exception as e:                            # al_video не сработал — пробуем страницу
        texts.append(("al_video error", str(e)))
    # 2) сама страница видео
    final, ctype, resp, head = _open_page(session, f"{VK_BASE}/video{vid}")
    text = _read_text(resp, head)
    _check_page(final, text)
    texts.append(("page", text))
    u, q = _pick_video(text, max_q)
    if u:
        return Resolved(url=u, name=f"video{vid}_{q}p.mp4", note=f"{q}p (страница)")
    joined = unescape_js("\n".join(t for _, t in texts))
    ext = EXTERNAL_VIDEO_RE.search(joined)
    if ext:
        raise PermanentError(f"внешнее видео ({ext.group(1).split('/')[0]}) — не хранится в VK")
    denied("\n".join(t for _, t in texts), debug, "video", url, "видео")
    if re.search(r'"hls(_raw|_ondemand)?"\s*:\s*"http', joined):
        debug.save("video", url, joined, "только HLS-поток")
        raise ParseError("видео доступно только потоком (HLS) — пока не поддерживается")
    debug.save("video", url, "\n\n<!-- ===== -->\n\n".join(f"<!-- {n} -->\n{t}" for n, t in texts),
               "не найдены ссылки на mp4")
    raise ParseError("не удалось найти файл видео")


def resolve(session, url, kind, debug, label=None, max_q=720):
    if kind == "doc_page":
        return resolve_doc(session, url, debug, label)
    if kind == "photo_page":
        return resolve_photo(session, url, debug, label)
    if kind == "video_page":
        return resolve_video(session, url, debug, label, max_q)
    raise ValueError(kind)
