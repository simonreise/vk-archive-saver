# -*- coding: utf-8 -*-
"""Запросы к API VK от имени веб-версии vk.ru.

Веб-версия VK встраивает в свои страницы токен (webToken) и сама ходит с ним
в API (web.api.vk.ru). Мы берём этот токен из страницы, открытой с вашими
cookie, и вызываем те же методы — например, messages.getById, чтобы получить
вложения сообщений вместе с ключами доступа (без них чужие документы и фото из
переписок VK не отдаёт).

Токен живёт ограниченное время; при истечении берём новый. В журналы и
отладочные файлы он не пишется.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from .downloader import retrying
from .vkauth import VK_BASE, USER_AGENT, LoginRequired, CaptchaRequired, is_login_page

API_VERSION = "5.289"
WEB_CLIENT_ID = "6287487"
TOKEN_PAGES = ("/feed", "/im", "/")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"API {code}: {msg}")
        self.code = code


def redact(text):
    """Убирает токены из текста (для журналов и отладочных файлов)."""
    text = re.sub(r'(access_token["\']?\s*[:=]\s*["\']?)[A-Za-z0-9._\-]{20,}', r"\1<REDACTED>", text)
    return re.sub(r"vk1\.a\.[A-Za-z0-9._\-]{20,}", "<REDACTED>", text)


class VKApi:
    def __init__(self, session):
        self.session = session
        self.token = None
        self.expires = 0
        self.domain = "web.api.vk.ru"
        self.lock = threading.Lock()

    # ------------------------------------------------------------ токен
    def _fetch_token(self):
        last = "страница VK не содержит webToken"
        for path in TOKEN_PAGES:
            final, ctype, body = self.session.get_page(VK_BASE + path)
            text = body.decode("utf-8", "replace")
            if is_login_page(final, text):
                raise LoginRequired("VK показывает страницу входа — cookie устарели. Загрузите свежие.")
            m = re.search(r'webToken\s*:\s*(\{[^{}]*\})', text)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            if data.get("is_anonym") or not data.get("access_token"):
                raise LoginRequired("VK выдал анонимный токен — вход по cookie не выполнен.")
            d = re.search(r'"apiDomain"\s*:\s*"([a-z0-9.\-]+)"', text)
            if d:
                self.domain = d.group(1)
            self.token = data["access_token"]
            self.expires = int(data.get("expired_at") or (time.time() + 600))
            return
        raise ApiError(0, last)

    def ensure_token(self, force=False):
        with self.lock:
            if force or not self.token or time.time() > self.expires - 60:
                self._fetch_token()
            return self.token

    # ------------------------------------------------------------ вызов
    def call(self, method, **params):
        for attempt in range(4):
            token = self.ensure_token(force=attempt > 0 and getattr(self, "_auth_failed", False))
            self._auth_failed = False
            body = dict(params, access_token=token)
            url = f"https://{self.domain}/method/{method}?v={API_VERSION}&client_id={WEB_CLIENT_ID}"
            req = urllib.request.Request(
                url, data=urllib.parse.urlencode(body).encode(),
                headers={"User-Agent": USER_AGENT, "Origin": VK_BASE, "Referer": VK_BASE + "/",
                         "Content-Type": "application/x-www-form-urlencoded"})
            self.session.throttle()

            def send():
                with self.session.opener.open(req, timeout=40) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            data = retrying(send, tries=3)
            if "response" in data:
                return data["response"]
            err = data.get("error") or {}
            code, msg = err.get("error_code", -1), err.get("error_msg", "?")
            if code == 5:                       # токен истёк/недействителен
                self._auth_failed = True
                continue
            if code in (6, 9, 29):              # слишком часто
                time.sleep(1.5 * (attempt + 1))
                continue
            if code == 14:
                raise CaptchaRequired("VK просит пройти капчу. Сделайте паузу и продолжите позже.")
            raise ApiError(code, msg)
        raise ApiError(code, msg)

    # ------------------------------------------------------------ вложения сообщений
    def message_attachments(self, message_ids):
        """{message_id: [attachment,...] или None, если сообщения нет}.
        Учитывает вложения пересланных сообщений и ответов."""
        out = {}
        ids = [int(i) for i in message_ids]
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            resp = self.call("messages.getById", message_ids=",".join(map(str, chunk)))
            items = resp.get("items", []) if isinstance(resp, dict) else resp
            for mid in chunk:
                out[mid] = None
            for it in items:
                atts = []
                _collect(it, atts)
                out[int(it.get("id", 0))] = atts
        return out


    def post_attachments(self, posts):
        """{"владелец_запись": [вложения] или None}; учитывает репосты (copy_history)."""
        out = {p: None for p in posts}
        posts = list(posts)
        for i in range(0, len(posts), 100):
            chunk = posts[i:i + 100]
            resp = self.call("wall.getById", posts=",".join(chunk))
            items = resp.get("items", []) if isinstance(resp, dict) else resp
            for it in items:
                atts = []
                _collect(it, atts)
                for c in it.get("copy_history") or []:
                    _collect(c, atts)
                out[f"{it.get('owner_id')}_{it.get('id')}"] = atts
        return out

    def comment_attachments(self, comments):
        """{"владелец_комментарий": [вложения] или None}."""
        out = {}
        for c in comments:
            own, cid = c.rsplit("_", 1)
            try:
                resp = self.call("wall.getComment", owner_id=own, comment_id=cid)
                items = resp.get("items", []) if isinstance(resp, dict) else resp
                out[c] = (items[0].get("attachments") or []) if items else None
            except ApiError as e:
                out[c] = None if e.code in (15, 30, 211, 212) else []
        return out


def _collect(msg, atts, depth=0):
    if depth > 5 or not isinstance(msg, dict):
        return
    atts.extend(msg.get("attachments") or [])
    for f in msg.get("fwd_messages") or []:
        _collect(f, atts, depth + 1)
    if msg.get("reply_message"):
        _collect(msg["reply_message"], atts, depth + 1)


# ------------------------------------------------------------ разбор вложений
def att_key(att):
    """('photo'|'doc'|'video', 'owner_id', object) для вложения."""
    t = att.get("type")
    obj = att.get(t) or {}
    if t in ("photo", "doc", "video") and "id" in obj:
        return t, f"{obj.get('owner_id')}_{obj.get('id')}", obj
    return None, None, None


def best_photo_url(photo):
    sizes = photo.get("sizes") or []
    if photo.get("orig_photo", {}).get("url"):
        sizes = sizes + [photo["orig_photo"]]
    if not sizes:
        return None
    best = max(sizes, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
    return best.get("url") or best.get("src")


def best_video_url(video, max_q):
    files = video.get("files") or {}
    found = {}
    for k, u in files.items():
        m = re.match(r"mp4_(\d+)$", k)
        if m and isinstance(u, str) and u.startswith("http"):
            found[int(m.group(1))] = u
    if not found:
        return None, None
    ok = [q for q in found if q <= max_q] or [min(found)]
    q = max(ok)
    return found[q], q
