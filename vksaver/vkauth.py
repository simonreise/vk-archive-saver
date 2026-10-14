# -*- coding: utf-8 -*-
"""Вход в VK по cookie из браузера.

Поддерживаемые форматы (определяются автоматически):
  * JSON из расширений Cookie-Editor / EditThisCookie (массив объектов);
  * cookies.txt в формате Netscape;
  * строка заголовка «name=value; name2=value2» (будет привязана к .vk.ru).

Cookie хранятся только локально, в файле vk_cookies.json в папке проекта.
Значения cookie никогда не пишутся в журнал.
"""
import base64
import http.cookiejar
import json
import re
import threading
import time
import urllib.request
from pathlib import Path

from .config import PROJECT_DIR

COOKIES_PATH = PROJECT_DIR / "vk_cookies.json"
VK_BASE = "https://vk.ru"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SESSION_COOKIES = ("remixsid", "remixnsid")       # без них вход не сработает


class CookieFormatError(Exception):
    pass


# ------------------------------------------------------------------ разбор
def parse_cookies(text):
    """-> список словарей {name, value, domain, path, secure, expires}."""
    text = text.strip().lstrip("﻿")
    if not text:
        raise CookieFormatError("Пустой файл.")
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise CookieFormatError(f"Не удалось прочитать JSON: {e}")
        if isinstance(data, dict):
            data = data.get("cookies") or [data]
        out = []
        for c in data:
            if not isinstance(c, dict) or "name" not in c:
                continue
            out.append({
                "name": str(c["name"]), "value": str(c.get("value", "")),
                "domain": c.get("domain") or ".vk.ru", "path": c.get("path") or "/",
                "secure": bool(c.get("secure", True)),
                "expires": int(c["expirationDate"]) if c.get("expirationDate") else None,
            })
        return out
    if "\t" in text:                                   # Netscape cookies.txt
        out = []
        for line in text.splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line or line.startswith("#"):
                continue
            p = line.split("\t")
            if len(p) >= 7:
                out.append({"name": p[5], "value": p[6], "domain": p[0], "path": p[2] or "/",
                            "secure": p[3].upper() == "TRUE",
                            "expires": int(p[4]) if p[4].isdigit() and p[4] != "0" else None})
        return out
    out = []                                           # name=value; name2=value2
    for part in re.split(r";\s*", text.replace("Cookie:", "").strip()):
        if "=" in part:
            n, v = part.split("=", 1)
            out.append({"name": n.strip(), "value": v.strip(), "domain": ".vk.ru",
                        "path": "/", "secure": True, "expires": None})
    if not out:
        raise CookieFormatError("Формат не распознан.")
    return out


def vk_only(cookies):
    return [c for c in cookies if re.search(r"(^|\.)vk\.(ru|com)$", c["domain"].lstrip("."))]


def summarize(cookies):
    """Безопасное описание (без значений) для показа пользователю."""
    names = {c["name"] for c in cookies}
    exp = [c["expires"] for c in cookies if c["name"] in SESSION_COOKIES and c["expires"]]
    domains = sorted({c["domain"].lstrip(".") for c in cookies})
    s = f"{len(cookies)} cookie для {', '.join(domains)}"
    if exp:
        s += ", сессия до " + time.strftime("%d.%m.%Y", time.localtime(min(exp)))
    missing = [n for n in SESSION_COOKIES if n not in names]
    if len(missing) == len(SESSION_COOKIES):
        s += ". ВНИМАНИЕ: нет remixsid/remixnsid — вход, скорее всего, не сработает"
    return s


# ------------------------------------------------------------------ хранение
def save_cookies(cookies):
    COOKIES_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")


def load_saved():
    if COOKIES_PATH.exists():
        try:
            return json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def forget():
    if COOKIES_PATH.exists():
        COOKIES_PATH.unlink()


# ------------------------------------------------------------------ сессия
class LoginRequired(Exception):
    """VK не считает нас вошедшими (cookie устарели или неполные)."""


class CaptchaRequired(Exception):
    """VK просит капчу — нужно сделать паузу."""


class VKSession:
    """HTTP-сессия с cookie. Cookie отправляются только на домены vk.ru/vk.com."""
    PAGE_GAP = 0.35                       # пауза между запросами страниц VK, с

    def __init__(self, cookies, user_id=None):
        self.user_id = user_id
        self.jar = http.cookiejar.CookieJar()
        for c in vk_only(cookies):
            dom = c["domain"]
            self.jar.set_cookie(http.cookiejar.Cookie(
                0, c["name"], c["value"], None, False, dom, True, dom.startswith("."),
                c["path"], True, c["secure"], c["expires"], c["expires"] is None,
                None, None, {}))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._lock = threading.Lock()
        self._last = 0.0

    def open(self, url, timeout=40, referer=VK_BASE + "/"):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Referer": referer,
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        return self.opener.open(req, timeout=timeout)

    def throttle(self):
        """Пауза между запросами страниц VK (общая для всех потоков)."""
        with self._lock:
            wait = self._last + self.PAGE_GAP - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def get_page(self, url, timeout=40):
        """Страница VK -> (final_url, content_type, bytes)."""
        self.throttle()
        with self.open(url, timeout) as r:
            return r.geturl(), (r.headers.get("Content-Type") or "").split(";")[0].lower(), r.read()

    def check(self):
        """-> (ok: bool, текст). Открывает ленту и ищет признаки входа."""
        final, ctype, body = self.get_page(VK_BASE + "/feed")
        text = body.decode("utf-8", "replace")
        if is_login_page(final, text):
            return False, "VK показывает страницу входа — cookie устарели или неполные."
        if self.user_id and str(self.user_id) not in text:
            return False, (f"Страница открылась, но ID {self.user_id} на ней не найден — "
                           "возможно, cookie от другого аккаунта или вход не выполнен.")
        m = re.search(r'<title>([^<]{1,80})</title>', text)
        return True, "Вход выполнен" + (f" ({m.group(1).strip()})" if m else "") + "."


def is_login_page(final_url, text):
    if re.search(r"/(login|join)\b|id\.vk\.(ru|com)/auth|oauth\.vk", final_url):
        return True
    return bool(re.search(r'id="(quick_login_form|index_login_form)"|name="login_form"|'
                          r'"isLoggedIn"\s*:\s*false|vk\.id\s*=\s*0\b', text))


def is_captcha_page(text):
    return bool(re.search(r"captcha_sid|challenge\.html|not_robot_captcha", text))


def archive_user_id(archive_root):
    """ID владельца архива из <meta name="jd"> в index.html."""
    try:
        head = (Path(archive_root) / "index.html").read_bytes()[:3000].decode("cp1251", "replace")
        m = re.search(r'name="jd" content="([^"]+)"', head)
        if m:
            s = m.group(1)
            data = json.loads(base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8"))
            return int(data.get("user_id"))
    except Exception:
        pass
    return None


def make_session(archive_root=None):
    """Сессия из сохранённых cookie или None."""
    cookies = load_saved()
    if not cookies:
        return None
    return VKSession(cookies, archive_user_id(archive_root) if archive_root else None)
