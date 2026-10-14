# -*- coding: utf-8 -*-
"""Имитация VK для тестов: обработчик urllib, отвечающий вместо настоящих серверов.

Все ID и данные выдуманы. Подключение:
    session = vkauth.VKSession(cookies, USER_ID)
    session.opener = urllib.request.build_opener(FakeVK(), HTTPCookieProcessor(session.jar))
"""
import email.message
import io
import json
import urllib.error
import urllib.parse
import urllib.request
import urllib.response

USER_ID = 1000001                 # «владелец архива»
FRIEND = 2000002                  # собеседник в личной переписке
GROUP = -3000003                  # сообщество
TOKEN = "vk1.a.FAKETOKENFAKETOKENFAKETOKENFAKETOKEN"

JPG = b"\xff\xd8\xff\xe0" + b"J" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"P" * 100
OGG = b"OggS" + b"O" * 200
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"M" * 300
PDF = b"%PDF-1.7 fake"


def resp(url, code=200, body=b"", ctype="text/html; charset=utf-8", headers=None):
    h = email.message.Message()
    h["Content-Type"] = ctype
    for k, v in (headers or {}).items():
        h[k] = v
    r = urllib.response.addinfourl(io.BytesIO(body), h, url, code)
    r.msg = "OK" if code == 200 else "Found"
    return r


def api_ok(url, data):
    return resp(url, body=json.dumps({"response": data}).encode(), ctype="application/json")


def api_err(url, code, msg):
    return resp(url, body=json.dumps({"error": {"error_code": code, "error_msg": msg}}).encode(),
                ctype="application/json")


def photo_obj(pid, owner, name, w=1280, h=960):
    return {"id": pid, "owner_id": owner, "access_key": "k",
            "sizes": [{"url": f"https://sun9-1.userapi.com/impg/{name}_s.jpg", "width": 130, "height": 100},
                      {"url": f"https://sun9-1.userapi.com/impg/{name}.jpg", "width": w, "height": h}]}


class FakeVK(urllib.request.BaseHandler):
    handler_order = 100                       # раньше стандартного HTTPSHandler

    def __init__(self):
        self.requests = []                    # (url, Cookie-заголовок или None)
        self.api_down = set()                 # методы API, которые «не отвечают» (таймаут)
        self.login_page = False               # имитация протухших cookie

    def https_open(self, req):
        u = req.full_url
        cookie = req.get_header("Cookie") or req.unredirected_hdrs.get("Cookie")
        self.requests.append((u, cookie))
        host = urllib.parse.urlsplit(u).hostname
        if self.login_page and host == "vk.ru":
            return resp(u, body=b'<form id="index_login_form"></form>')

        # ---------- страница с webToken
        if u.startswith("https://vk.ru/feed"):
            return resp(u, body=('<script>window.x={apiConfigDomains: {"apiDomain":"web.api.vk.ru"}, '
                                 'webToken: {"access_token":"%s","expired_at":9999999999,"is_anonym":false}}; '
                                 'var vk={id: %d}</script><title>Feed</title>' % (TOKEN, USER_ID)).encode())

        # ---------- API
        if host == "web.api.vk.ru":
            method = urllib.parse.urlsplit(u).path.rsplit("/", 1)[-1]
            if method in self.api_down:
                raise urllib.error.URLError("timed out (fake)")
            p = {k: v[0] for k, v in urllib.parse.parse_qs(req.data.decode()).items()}
            return self.api(u, method, p)

        # ---------- страницы VK
        if u == f"https://vk.ru/doc{USER_ID}_10":                     # сразу редирект на файл
            return resp(u, 302, headers={"Location": "https://psv4.userapi.com/c1/docs/d1/Lecture%201.pdf?extra=a"})
        if u.startswith(f"https://vk.ru/doc{FRIEND}_20?hash="):        # страница документа с docUrl (d2)
            return resp(u, body=b'<script>Docs.initDoc({"docTitle":"report.pdf","docUrl":'
                                b'"https:\\/\\/psv4.vkuserphoto.ru\\/s\\/v1\\/d2\\/AAA\\/report.pdf"})</script>')
        if u == f"https://vk.ru/doc{USER_ID}_30":                      # профиль: страница с docUrl
            return resp(u, body=b'<script>Docs.initDoc({"docUrl":"https:\\/\\/psv4.vkuserphoto.ru\\/s\\/v1\\/d\\/BBB\\/x.docx"})</script>')
        if u == f"https://vk.ru/doc{USER_ID}_31":
            return resp(u, body="<div>Ошибка</div><div>Файл недоступен или удалён</div>".encode())
        if u.startswith("https://vk.ru/photo"):          # как у настоящего VK для фото из переписок
            return resp(u, body=b'<script>window.cur.apiPrefetchCache = [{"method":"photos.getById",'
                                b'"response":null,"error":{"error_code":200,"error_msg":"Access denied"}}];</script>')
        if u.startswith("https://vk.ru/al_video.php"):
            return resp(u, body=b'{"payload":["8",["\\"\\u0414\\u043e\\u0441\\u0442\\u0443\\u043f\\"]]}')
        if u.startswith("https://vk.ru/video"):
            return resp(u, body=b"<div></div>")

        # ---------- файлы
        if host.endswith("userapi.com") or host.endswith("vkuserphoto.ru") or host.endswith("mycdn.me"):
            path = urllib.parse.urlsplit(u).path
            if path.endswith(".ogg"):
                return resp(u, body=OGG, ctype="audio/ogg")
            if path.endswith(".mp4"):
                return resp(u, body=MP4, ctype="video/mp4")
            if path.endswith(".pdf"):
                return resp(u, body=PDF, ctype="application/pdf",
                            headers={"Content-Disposition": "attachment; filename*=UTF-8''%D0%9B%D0%B5%D0%BA%D1%86%D0%B8%D1%8F.pdf"})
            if path.endswith(".docx"):
                return resp(u, body=b"PK\x03\x04docx", ctype="application/octet-stream")
            if "dead" in path:
                raise urllib.error.HTTPError(u, 424, "Failed Dependency", None, None)
            return resp(u, body=JPG, ctype="image/jpeg")
        raise urllib.error.HTTPError(u, 404, "Not Found", None, None)

    # ------------------------------------------------------------ методы API
    def api(self, u, method, p):
        if method == "messages.getById":
            items = []
            for mid in map(int, p["message_ids"].split(",")):
                if mid == 101:      # фото по ссылке im?z=photo…
                    items.append({"id": 101, "attachments": [{"type": "photo", "photo": photo_obj(77, FRIEND, "msgphoto")}]})
                elif mid == 102:    # чужой документ: API даёт ссылку на страницу с hash
                    items.append({"id": 102, "attachments": [{"type": "doc", "doc": {
                        "id": 20, "owner_id": FRIEND, "title": "Отчёт", "ext": "pdf",
                        "url": f"https://vk.ru/doc{FRIEND}_20?hash=H&dl=D"}}]})
                elif mid == 103:    # видео с файлами
                    items.append({"id": 103, "attachments": [{"type": "video", "video": {
                        "id": 88, "owner_id": FRIEND, "files": {"mp4_360": "https://vkvd1.mycdn.me/v360.mp4",
                                                                "mp4_1080": "https://vkvd1.mycdn.me/v1080.mp4"}}}]})
                elif mid == 104:    # сообщение есть, вложение удалено
                    items.append({"id": 104, "attachments": []})
                # 105 — сообщение удалено (не возвращается)
            return api_ok(u, {"count": len(items), "items": items})
        if method == "wall.getById":
            return api_ok(u, {"items": [{"id": 50, "owner_id": USER_ID, "attachments": [], "copy_history": [
                {"id": 9, "owner_id": GROUP, "attachments": [{"type": "photo", "photo": photo_obj(66, GROUP, "repost")}]}]}]})
        if method == "wall.getComment":
            return api_ok(u, {"items": [{"id": 51, "attachments": [{"type": "photo", "photo": photo_obj(55, USER_ID, "comment")}]}]})
        if method == "photos.getById":
            if p["photos"] == f"{GROUP}_401":
                return api_ok(u, [photo_obj(401, GROUP, "liked")])
            if p["photos"] == f"{GROUP}_402" or p["photos"].startswith(f"{FRIEND}_"):
                return api_err(u, 200, "Access denied")   # чужие фото из переписок — только с ключом
            return api_ok(u, [])
        if method == "video.get":
            if p["videos"].startswith(f"{USER_ID}_600"):
                return api_ok(u, {"items": [{"id": 600, "owner_id": USER_ID, "files": {"mp4_480": "https://vkvd1.mycdn.me/alb480.mp4"}}]})
            if p["videos"].startswith(f"{USER_ID}_601"):
                return api_ok(u, {"items": [{"id": 601, "owner_id": USER_ID, "platform": "YouTube"}]})
            return api_ok(u, {"items": []})
        return api_err(u, 15, "Access denied")
