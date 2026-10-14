# -*- coding: utf-8 -*-
"""Тесты без сети: выдуманный архив во временной папке + имитация VK (fakevk.py).

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakevk  # noqa: E402
from fakevk import USER_ID, FRIEND, GROUP  # noqa: E402
from vksaver import vkauth, htmlmedia, config  # noqa: E402
from vksaver.common import classify  # noqa: E402
from vksaver.engine import Engine  # noqa: E402
from vksaver.vkapi import redact  # noqa: E402
import vksaver.downloader as downloader  # noqa: E402

downloader.TRIES = 2          # тесты не должны долго ждать повторов
downloader.RETRY_DELAY = 0.01


# ------------------------------------------------------------------ выдуманный архив
def att(desc, url):
    return (f'<div class="attachment">\n  <div class="attachment__description">{desc}</div>\n'
            f"  <a class='attachment__link' href='{url}'>{url}</a>\n</div>")


def msg(mid, body):
    return (f'<div class="item"><div class=\'item__main\'><div class="message" data-id="{mid}">'
            f'<div class="message__header">Вы, 1 янв 2020</div><div><div class="kludges">{body}'
            f'</div></div></div></div></div>')


def page(body):
    return ('<!DOCTYPE html><html><head><meta charset="windows-1251"></head><body>'
            f'<div class="wrap_page_content">{body}</div></body></html>').encode("cp1251")


def build_archive(root):
    root = Path(root)
    jd = base64.b64encode(json.dumps({"user_id": USER_ID}).encode()).decode().rstrip("=")
    (root / "index.html").write_bytes(f'<meta charset="windows-1251"><meta name="jd" content="{jd}">'.encode())
    files = {
        # личная переписка: прямые ссылки + ссылки на страницы VK
        f"messages/{FRIEND}/messages0.html": page(
            msg(1, att("Фотография", "https://sun9-1.userapi.com/impg/a/direct.jpg?size=10x10&amp;sign=1"))
            + msg(2, att("Файл", "https://psv4.userapi.com/s/v1/amsg/voice1.ogg"))
            + msg(3, att("Фотография", "https://sun9-1.userapi.com/impg/a/dead.jpg?size=1x1"))
            + msg(101, att("Фотография", f"https://vk.ru/im?sel={FRIEND}&z=photo{FRIEND}_77%2Fmail101"))
            + msg(102, att("Файл", f"https://vk.ru/doc{FRIEND}_20"))
            + msg(103, att("Видеозапись", f"https://vk.ru/video{FRIEND}_88"))
            + msg(104, att("Файл", f"https://vk.ru/doc{FRIEND}_21"))
            + msg(105, att("Фотография", f"https://vk.ru/im?sel={FRIEND}&z=photo{FRIEND}_99%2Fmail105"))),
        # беседа
        "messages/2000000001/messages0.html": page(
            msg(7, att("Фотография", "https://sun9-1.userapi.com/impg/a/group.jpg?size=10x10"))
            + msg(103, att("Видеозапись", f"https://vk.ru/video{FRIEND}_88"))),
        # стена: репост и комментарий
        "wall/wall0.html": page(
            '<div class="item">' + att("Фотография", f"https://vk.ru/photo{GROUP}_66")
            + f'<a class="post__link" href="https://vk.ru/wall{USER_ID}_50">x</a></div>'),
        "wall/50/comments0.html": page(
            '<div class="item">' + att("Фотография", f"https://vk.ru/photo{USER_ID}_55")
            + f'<a class="post__link" href="https://vk.ru/wall{USER_ID}_50?reply=51">x</a></div>'),
        # профиль: альбом (img src), документы (обычные ссылки), видеоальбом
        "photos/photo-albums/-15.html": page(
            f'<a href="https://vk.ru/photo{USER_ID}_1"><img src="https://sun9-1.userapi.com/impg/a/album.jpg?size=5x5" alt=""></a>'
            f'<a href="https://vk.ru/photo{USER_ID}_1">vk.ru/photo{USER_ID}_1</a>'),
        "profile/documents.html": page(
            f'<div class="item"><a href="https://vk.ru/doc{USER_ID}_10">Конспект лекции.pdf</a></div>'
            f'<div class="item"><a href="https://vk.ru/doc{USER_ID}_30">План.docx</a></div>'
            f'<div class="item"><a href="https://vk.ru/doc{USER_ID}_31">Удалённый.txt</a></div>'),
        "video/video-albums/-1.html": page(
            f'<a href="https://vk.ru/video{USER_ID}_600">\n<div class="video-thumb"></div>\n</a>'
            f'<div><a href="https://vk.ru/video{USER_ID}_600">vk.ru/video{USER_ID}_600</a></div>'
            f'<div><a href="https://vk.ru/video{USER_ID}_601">vk.ru/video{USER_ID}_601</a></div>'),
        # лайки
        "likes/likes.html": page('<a href="photo/likes0.html">Фото</a>'),
        "likes/photo/likes0.html": page(
            f'<div class="item"><a href="https://vk.ru/photo{GROUP}_401">https://vk.ru/photo{GROUP}_401</a></div>'
            f'<div class="item"><a href="https://vk.ru/photo{GROUP}_402">https://vk.ru/photo{GROUP}_402</a></div>'),
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def make_session():
    cookies = [{"name": "remixsid", "value": "SECRET", "domain": ".vk.ru", "path": "/",
                "secure": True, "expires": None}]
    s = vkauth.VKSession(cookies, USER_ID)
    fake = fakevk.FakeVK()
    s.opener = urllib.request.build_opener(fake, urllib.request.HTTPCookieProcessor(s.jar))
    s.PAGE_GAP = 0
    return s, fake


def text(root, rel):
    return (Path(root) / rel).read_bytes().decode("cp1251")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vksaver_test_")
        self.root = build_archive(self.tmp)
        self.session, self.fake = make_session()
        self.logs = []
        self.eng = Engine(self.root, log=self.logs.append, session=self.session, max_video_q=720)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stats(self, sid):
        rows, _ = self.eng.stats(sid)
        return rows


# ------------------------------------------------------------------ тесты
class TestClassify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify("https://sun9-1.userapi.com/impg/a/b.jpg?size=1x1"), "photo")
        self.assertEqual(classify("https://psv4.userapi.com/s/v1/amsg/x.ogg"), "voice")
        self.assertEqual(classify("https://vk.ru/doc1_2"), "doc_page")
        self.assertEqual(classify("https://vk.ru/video-1_2"), "video_page")
        self.assertEqual(classify("https://vk.ru/im?sel=1&z=photo1_2%2Fmail3"), "photo_page")
        self.assertIsNone(classify("https://vk.ru/id1"))
        # обычные ссылки на фото учитываются только в лайках/закладках
        self.assertIsNone(classify("https://vk.ru/photo1_2", plain_href=True))
        self.assertEqual(classify("https://vk.ru/photo1_2", plain_href=True, photo_pages=True), "photo_page")


class TestDownload(Base):
    def test_messages_full_cycle(self):
        self.eng.download("messages")
        r = self.stats("messages")
        self.assertEqual(r["voice"]["local"], 1)
        self.assertEqual(r["photo"]["local"], 3)      # прямое + из API + беседа
        self.assertEqual(r["doc"]["local"], 1)        # чужой документ со страницы с hash
        self.assertEqual(r["video"]["local"], 2)      # в двух переписках
        self.assertEqual(r["photo"]["dead"], 2)       # 424 и удалённое сообщение
        self.assertEqual(r["doc"]["dead"], 1)         # вложение удалено из сообщения
        p = text(self.root, f"messages/{FRIEND}/messages0.html")
        self.assertIn("<audio class='attachment__voice'", p)
        self.assertIn("Голосовое сообщение", p)
        self.assertIn("src='video/video", p)
        self.assertIn("_360p.mp4", p)                  # 1080p > ограничения 720p
        self.assertTrue((self.root / f"messages/{FRIEND}/docs/Отчёт.pdf").exists())
        self.assertTrue((self.root / f"_backup_html/messages/{FRIEND}/messages0.html").exists())
        # повторный запуск ничего не скачивает и не меняет
        before = p
        self.eng.download("messages")
        self.assertIn("Скачано: ничего", self.logs[-1] if "Скачано" in self.logs[-1] else self.logs[-2])
        self.assertEqual(before, text(self.root, f"messages/{FRIEND}/messages0.html"))

    def test_chat_categories(self):
        self.eng.download("messages", kinds={"personal": ["photo"], "group": [], "community": []})
        r = self.stats("messages")
        self.assertEqual(r["_cats"]["group"]["photo"], 1)        # беседа не тронута
        self.assertFalse((self.root / "messages/2000000001/photos").exists())

    def test_wall_repost_and_comment(self):
        self.eng.download("wall")
        self.assertTrue((self.root / "wall/photos/repost.jpg").exists())
        self.assertTrue((self.root / "wall/50/photos/comment.jpg").exists())

    def test_profile(self):
        self.eng.download("profile", kinds=["photo", "doc", "video"])
        r = self.stats("profile")
        self.assertEqual(r["photo"]["local"], 1)
        self.assertEqual(r["doc"]["local"], 2)
        self.assertEqual(r["doc"]["dead"], 1)
        self.assertEqual(r["video"]["local"], 1)
        self.assertEqual(r["video"]["dead"], 1)      # YouTube
        self.assertTrue((self.root / "profile/docs/Конспект лекции.pdf").exists())
        doc = text(self.root, "profile/documents.html")
        self.assertIn('data-vks="1" href="https://vk.ru/doc', doc)     # исходная ссылка сохранена
        alb = text(self.root, "video/video-albums/-1.html")
        self.assertEqual(alb.count("<video"), 1)                         # превью не задвоило плеер

    def test_likes_keep_links(self):
        self.eng.download("likes", kinds=["photo"])
        p = text(self.root, "likes/photo/likes0.html")
        self.assertIn("src='photos/liked.jpg'", p)
        self.assertIn(f'data-vks="1" href="https://vk.ru/photo{GROUP}_401"', p)
        r = self.stats("likes")
        self.assertEqual((r["photo"]["local"], r["photo"]["dead"]), (1, 1))
        self.assertEqual(r["video"]["total"], 0)           # «likes/photo/…html» — не видео

    def test_rebuild_is_idempotent(self):
        self.eng.download("likes", kinds=["photo"])
        once = text(self.root, "likes/photo/likes0.html")
        self.eng.rebuild("likes")
        self.eng.rebuild("likes")
        self.assertEqual(once, text(self.root, "likes/photo/likes0.html"))

    def test_api_outage_is_temporary(self):
        # messages.getById не отвечает; фото из переписки без ключа VK не отдаёт ни через
        # photos.getById, ни через страницу — ссылка не должна стать «недоступной»
        self.fake.api_down = {"messages.getById"}
        self.eng.download("messages", kinds=["photo"])
        r = self.stats("messages")
        self.assertEqual(r["photo"]["dead"], 1)    # только настоящий 424
        self.assertEqual(r["photo"]["retry"], 2)
        self.fake.api_down = set()
        self.eng.download("messages", kinds=["photo"])
        self.assertEqual(self.stats("messages")["photo"]["local"], 3)

    def test_login_page_aborts(self):
        self.fake.login_page = True
        self.eng.download("profile", kinds=["doc"])
        self.assertTrue(self.eng.abort_reason)
        self.assertEqual(self.stats("profile")["doc"]["dead"], 0)   # ничего не помечено мёртвым

    def test_cookies_only_to_vk(self):
        self.eng.download("messages")
        for url, cookie in self.fake.requests:
            host = url.split("/")[2]
            if not (host == "vk.ru" or host.endswith(".vk.ru")):
                self.assertIsNone(cookie, url)


class TestCookiesAndConfig(unittest.TestCase):
    def test_formats(self):
        js = json.dumps([{"domain": ".vk.ru", "name": "remixsid", "value": "A", "path": "/", "secure": True,
                          "expirationDate": 1900000000}, {"domain": ".example.com", "name": "x", "value": "y"}])
        for t in (js, "# Netscape\n.vk.ru\tTRUE\t/\tTRUE\t1900000000\tremixsid\tA\n", "remixsid=A; remixnsid=B"):
            c = vkauth.vk_only(vkauth.parse_cookies(t))
            self.assertTrue(c and all("vk.ru" in x["domain"] for x in c))

    def test_redact(self):
        s = redact('webToken: {"access_token":"%s"}' % fakevk.TOKEN)
        self.assertNotIn(fakevk.TOKEN, s)

    def test_settings_migration(self):
        s = {"kinds": {"other": {"own": {"photo": True, "doc": True, "video": True},
                                 "likes": {"photo": False, "doc": True, "video": False}}}}
        config._migrate_kinds(s)
        self.assertEqual(s["kinds"]["profile"]["all"]["video"], True)
        self.assertEqual(s["kinds"]["likes"]["all"]["photo"], False)


if __name__ == "__main__":
    unittest.main()
