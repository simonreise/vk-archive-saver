# -*- coding: utf-8 -*-
"""Поиск медиа-ссылок в HTML выгрузки VK и замена их на локальные файлы.

Работаем с байтами: страницы в windows-1251, и так ничего не испортится.
"""
import html as htmlmod
import re
from urllib.parse import quote, unquote

from .common import HTML_ENCODING, classify, local_kind, AUDIO_EXT, IMAGE_EXT

# вложение в сообщении/записи: <a class='attachment__link' href='URL'>текст</a>
ATT_A = re.compile(rb"<a class='attachment__link' href='([^']+)'>([^<]*)</a>")
# картинка (фотоальбомы, профиль): <img ... src="URL" ...>
IMG_SRC = re.compile(rb"""(<img\b[^>]*?\bsrc=)(["'])([^"']+)\2""")
ANY_HREF = re.compile(rb"""\b(?:href|src)=(["'])([^"']+)\1""")
# обычная ссылка (профиль, видеоальбомы): <a href="URL">текст</a>
PLAIN_A = re.compile(rb"""(<a\b[^>]*?\bhref=)(["'])(https?://[^"']+)\2([^>]*>)([^<]*)(</a>)?""")
MSG_ID = re.compile(rb'<div class="message" data-id="(\d+)"')
# ссылка на запись/комментарий стены, идущая после её содержимого
WALL_LINK = re.compile(rb'href="https?://vk\.(?:ru|com)/wall(-?\d+)_(\d+)(?:\?reply=(\d+))?"')
VOICE_DESC = re.compile(rb"(<div class=\"attachment__description\">)[^<]*(</div>\s*<audio class='attachment__voice')")

KEPT_MARK = b'data-vks="1"'                  # исходная ссылка, рядом с которой уже есть файл
VOICE_LABEL = "Голосовое сообщение".encode(HTML_ENCODING)
IMG_STYLE = ("display:block;max-width:100%;max-height:480px;"
             "margin-top:4px;border-radius:8px;cursor:zoom-in")
AUDIO_STYLE = "display:block;width:320px;max-width:100%;height:40px;margin-top:4px"
VIDEO_STYLE = "display:block;max-width:100%;max-height:480px;margin-top:4px;border-radius:8px;background:#000"
VIDEO_EXT = (".mp4", ".webm", ".m4v", ".mov")


def _url(b):
    return b.decode(HTML_ENCODING, "replace").replace("&amp;", "&")


def extract(raw, photo_pages=False):
    """-> (remote: {url: kind}, local: {kind: set(rel_path)}, labels: {url: текст ссылки},
           mids: {url: id сообщения (int) или "w:владелец_запись" / "c:владелец_комментарий"})"""
    remote, local, labels, mids = {}, {}, {}, {}
    starts = [(m.start(), int(m.group(1))) for m in MSG_ID.finditer(raw)]
    walls = [] if starts else [(m.start(), m.group(1).decode(), m.group(2).decode(),
                                m.group(3).decode() if m.group(3) else None)
                               for m in WALL_LINK.finditer(raw)]
    wi = 0
    si, cur = 0, None
    for m in ATT_A.finditer(raw):
        while si < len(starts) and starts[si][0] < m.start():
            cur = starts[si][1]
            si += 1
        u = _url(m.group(1))
        k = classify(u)
        if k:
            remote[u] = k
            if cur is not None and k.endswith("_page"):
                mids.setdefault(u, cur)
            elif walls and k.endswith("_page"):
                while wi < len(walls) and walls[wi][0] < m.start():
                    wi += 1
                if wi < len(walls):
                    _, own, post, reply = walls[wi]
                    mids.setdefault(u, f"c:{own}_{reply}" if reply else f"w:{own}_{post}")
    for m in IMG_SRC.finditer(raw):
        u = _url(m.group(3))
        if classify(u) == "photo":
            remote.setdefault(u, "photo")
    for m in ANY_HREF.finditer(raw):
        v = _url(m.group(2))
        lk = local_kind(v)
        if lk:
            local.setdefault(lk, set()).add(unquote(v))
            continue
        if v.startswith("http"):
            k = classify(v, plain_href=True, photo_pages=photo_pages)
            if k:
                remote.setdefault(v, k)
    for m in PLAIN_A.finditer(raw):            # подписи: имена документов в профиле
        u = _url(m.group(3))
        if u in remote and remote[u] == "doc_page":
            t = htmlmod.unescape(m.group(5).decode(HTML_ENCODING, "replace")).strip()
            if t and not t.startswith(("http", "vk.ru/", "vk.com/")):
                labels[u] = t[:150]
    return remote, local, labels, mids


def href(rel):
    """Относительный путь для атрибута href/src (не-ASCII символы — в %XX)."""
    return quote(rel, safe="/()-_.~ ").replace(" ", "%20")


def _esc(s):
    return htmlmod.escape(s, quote=True)


def media_tag(kind, rel, label=""):
    name = label or rel.rsplit("/", 1)[-1]
    ext = ("." + rel.rsplit(".", 1)[-1].lower()) if "." in rel.rsplit("/", 1)[-1] else ""
    r = href(rel)
    if kind == "photo" or (kind == "doc" and ext in IMAGE_EXT):
        s = (f"<a class='attachment__link' href='{r}' target='_blank'>"
             f"<img class='attachment__photo' src='{r}' loading='lazy' alt='photo' "
             f"style='{IMG_STYLE}'></a>")
        if kind == "doc":
            s += f"<a class='attachment__link attachment__file' href='{r}' download>{_esc(name)}</a>"
    elif kind == "voice" or (kind == "doc" and ext in AUDIO_EXT):
        cls = "attachment__voice" if kind == "voice" else "attachment__audio"
        s = (f"<audio class='{cls}' controls preload='none' src='{r}' "
             f"style='{AUDIO_STYLE}'></audio>"
             f"<a class='attachment__link' href='{r}' download>"
             f"{'скачать' if kind == 'voice' else _esc(name)}</a>")
    elif kind == "video" or (kind == "doc" and ext in VIDEO_EXT):
        s = (f"<video class='attachment__video' controls preload='metadata' src='{r}' "
             f"style='{VIDEO_STYLE}'></video>"
             f"<a class='attachment__link' href='{r}' download>{_esc(name)}</a>")
    else:
        s = f"<a class='attachment__link attachment__file' href='{r}' download>{_esc(name)}</a>"
    return s.encode(HTML_ENCODING, "replace")


def rewrite(raw, resolved):
    """resolved: {url: (kind, rel_path, label)} -> (new_bytes, replaced_count)"""
    count = [0]

    def att(m):
        r = resolved.get(_url(m.group(1)))
        if not r:
            return m.group(0)
        count[0] += 1
        return media_tag(r[0], r[1], r[2] if len(r) > 2 else "")

    def img(m):
        r = resolved.get(_url(m.group(3)))
        if not r:
            return m.group(0)
        count[0] += 1
        q = m.group(2)
        return m.group(1) + q + href(r[1]).encode("ascii") + q

    def plain(m):
        # обычная ссылка на страницу VK: файл вставляется ПЕРЕД ней, сама ссылка остаётся
        if KEPT_MARK in m.group(1):
            return m.group(0)                          # уже обработана раньше
        r = resolved.get(_url(m.group(3)))
        if not r or not m.group(6) or not m.group(5).strip():
            return m.group(0)                          # превью без текста и т.п. — не трогаем
        count[0] += 1
        kept = m.group(1).replace(b"<a", b"<a " + KEPT_MARK, 1) + m.group(0)[len(m.group(1)):]
        return media_tag(r[0], r[1], r[2] if len(r) > 2 else "") + b"<br>" + kept

    new = ATT_A.sub(att, raw)
    new = IMG_SRC.sub(img, new)
    new = PLAIN_A.sub(plain, new)
    if count[0]:
        new = VOICE_DESC.sub(rb"\1" + VOICE_LABEL + rb"\2", new)
    return new, count[0]
