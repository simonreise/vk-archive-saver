# -*- coding: utf-8 -*-
"""Сканирование раздела и загрузка медиа. Не зависит от GUI.

Обратная связь — через колбэки:
  log(text)                      — строка в журнал
  progress(done, total, text)    — ход текущей операции
Остановка — threading.Event (stop.set()).
"""
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import htmlmedia
from .common import (KINDS, FILE_KINDS, WORK_DIR, BACKUP_DIR, rel_posix, fmt_size,
                     target, needs_login)
from .downloader import (open_url, stream_to_temp, server_filename, pick_name, retrying,
                         PermanentError, Stopped)
from .resolvers import resolve, ParseError, DebugSaver
from .sections import SECTIONS, SECTIONS_BY_ID, scope_category, categories
from .store import ScopeState, load_json, save_json
from .vkauth import LoginRequired, CaptchaRequired
from .vkapi import VKApi, ApiError, att_key, best_photo_url, best_video_url
from .resolvers import Resolved
import re


class TemporaryError(Exception):
    """Временная ошибка: ссылка будет повторена при следующем запуске."""

MAX_TEMP_RUNS = 2       # после стольких запусков с временной ошибкой ссылка считается мёртвой
CACHE_VERSION = 3
EXTRACT_VERSION = 2     # версия разбора HTML; старые записи (кроме сообщений) перечитываются

STAT_COLUMNS = [        # ключ, заголовок
    ("total", "Всего"),
    ("local", "Сохранено"),
    ("pending", "Ждёт загрузки"),
    ("login", "Через вход в VK"),
    ("retry", "Врем. ошибки"),
    ("dead", "Недоступно"),
]


def _noop(*a, **k):
    pass


class Engine:
    def __init__(self, root, log=None, progress=None, stop=None, workers=6,
                 session=None, max_video_q=720):
        self.root = Path(root)
        self.log = log or _noop
        self.progress = progress or _noop
        self.stop = stop or threading.Event()
        self.workers = workers
        self.session = session
        self.max_video_q = max_video_q
        self.work = self.root / WORK_DIR
        self.abort_reason = None
        self.api_failures = 0

    # ------------------------------------------------------------------ кэш
    def _cache_path(self, sid):
        return self.work / f"scan_{sid}.json"

    def _empty_cache(self):
        return {"version": CACHE_VERSION, "files": {}, "scanned_at": None}

    def load_cache(self, sid, any_version=False):
        c = load_json(self._cache_path(sid))
        if c.get("version") == CACHE_VERSION or (any_version and c.get("version") in (2, 3)):
            return c
        return self._empty_cache()

    def save_cache(self, sid, cache):
        save_json(self._cache_path(sid), cache)

    @staticmethod
    def _photo_pages(rel):
        """Учитывать ли обычные ссылки на фото (лайки и закладки)."""
        return rel.startswith(("likes/", "bookmarks/"))

    def _entry(self, path, raw=None, rel=None):
        st = path.stat()
        if raw is None:
            raw = path.read_bytes()
        pp = self._photo_pages(rel or rel_posix(path, self.root))
        remote, local, labels, mids = htmlmedia.extract(raw, photo_pages=pp)
        e = {"m": st.st_mtime, "s": st.st_size, "r": remote,
             "l": {k: sorted(v) for k, v in local.items()}}
        e["x"] = EXTRACT_VERSION
        if labels:
            e["n"] = labels
        if mids:
            e["i"] = mids
        return e

    # ------------------------------------------------------------ сканирование
    def scan(self, sid, full=False):
        """Обновляет кэш раздела (читает только изменившиеся файлы)."""
        sec = SECTIONS_BY_ID[sid]
        cache = self._empty_cache() if full else self.load_cache(sid)
        old = cache["files"]
        self.progress(0, 0, "поиск файлов…")
        files = sec.html_files(self.root)
        new_files, read, i = {}, 0, 0
        t0 = time.time()
        for i, p in enumerate(files, 1):
            if self.stop.is_set():
                self.log("Сканирование остановлено.")
                break
            rel = rel_posix(p, self.root)
            try:
                st = p.stat()
                e = old.get(rel)
                if e and e["m"] == st.st_mtime and e["s"] == st.st_size and \
                        (e.get("x", 1) >= EXTRACT_VERSION or rel.startswith("messages/")):
                    new_files[rel] = e
                else:
                    new_files[rel] = self._entry(p, rel=rel)
                    read += 1
            except OSError as ex:
                self.log(f"Не удалось прочитать {rel}: {ex}")
            if i % 50 == 0 or i == len(files):
                self.progress(i, len(files), rel)
        else:
            cache["scanned_at"] = time.strftime("%Y-%m-%d %H:%M")
        stopped = self.stop.is_set()
        if stopped:
            new_files = dict(old, **new_files)
        cache["files"] = new_files
        self.save_cache(sid, cache)
        self.log(f"[{sec.title}] просмотрено файлов: {i if stopped else len(files)} из {len(files)}, "
                 f"прочитано заново: {read}, за {time.time() - t0:.0f} с")
        return cache

    # ------------------------------------------------------------ статистика
    def _scopes(self, cache):
        """{папка: {"files": [rel], "remote": {url: kind}, "local": {kind: set}, "labels": {}}}"""
        scopes = {}
        for rel, e in cache["files"].items():
            d = rel.rsplit("/", 1)[0] if "/" in rel else ""
            sc = scopes.setdefault(d, {"files": [], "remote": {}, "local": {}, "labels": {}, "mids": {}})
            sc["files"].append(rel)
            for u, k in e["r"].items():
                sc["remote"].setdefault(u, k)
            for k, lst in e["l"].items():
                sc["local"].setdefault(k, set()).update(
                    x for x in lst if not x.lower().endswith((".html", ".htm")))
            sc["labels"].update(e.get("n", {}))
            sc["mids"].update(e.get("i", {}))
        return scopes

    def stats(self, sid, cache=None):
        cache = cache or self.load_cache(sid)
        rows = {k: {c: 0 for c, _ in STAT_COLUMNS} for k in FILE_KINDS}
        cats = {c: {k: 0 for k in FILE_KINDS} for c, _ in categories(sid)}   # осталось скачать
        for d, sc in self._scopes(cache).items():
            cat = cats.get(scope_category(sid, d))
            state = None
            for k, paths in sc["local"].items():
                rows[k]["local"] += len(paths)
            for u, k in sc["remote"].items():
                r = rows[target(k)]
                if state is None:
                    state = ScopeState(self.root / d)
                if needs_login(k) and state.local_name(u, target(k)):
                    continue                        # ссылка оставлена рядом со скачанным файлом
                if u in state.failed:
                    r["dead"] += 1
                elif u in state.retry:
                    r["retry"] += 1
                elif needs_login(k):
                    r["login"] += 1
                else:
                    r["pending"] += 1
                if cat is not None and u not in state.failed:
                    cat[target(k)] += 1
        rows["_cats"] = cats
        for r in (rows[k] for k in FILE_KINDS):
            r["total"] = sum(v for c, v in r.items() if c != "total")
        return rows, cache

    # ------------------------------------------------------------ загрузка
    def download(self, sid, kinds=None, retry_failed=False):
        """kinds — какие виды файлов качать (photo/voice/doc/video): список для всего
        раздела или словарь {категория: список} (для сообщений: personal/group/community).
        По умолчанию — всё."""
        sec = SECTIONS_BY_ID[sid]
        if isinstance(kinds, dict):
            by_cat = {c: set(v) for c, v in kinds.items()}
        else:
            by_cat = {c: set(kinds or FILE_KINDS) for c, _ in categories(sid)}
        cache = self.scan(sid)
        if self.stop.is_set():
            return
        self.work.mkdir(exist_ok=True)
        err_path = self.work / f"errors_{sid}.log"
        self.debug = DebugSaver(self.work / "debug")
        self.abort_reason = None
        self.api = VKApi(self.session) if self.session else None
        self.api_failures = 0
        skipped_login = 0

        plan = []
        for d, sc in sorted(self._scopes(cache).items()):
            wanted = {}
            kinds = by_cat.get(scope_category(sid, d), set())
            for u, k in sc["remote"].items():
                if target(k) not in kinds:
                    continue
                if needs_login(k) and not self.session:
                    skipped_login += 1
                    continue
                wanted[u] = k
            if not wanted:
                continue
            state = ScopeState(self.root / d)
            if retry_failed:
                state.failed, state.retry = {}, {}
            todo = [(u, k) for u, k in wanted.items()
                    if not state.local_name(u, target(k)) and u not in state.failed]
            plan.append((d, sc, wanted, state, todo))
        total = sum(len(p[4]) for p in plan)
        def kn(ks):
            return ", ".join(KINDS[k]["title"].lower() for k in FILE_KINDS if k in ks) or "ничего"
        if len(by_cat) == 1:
            names = kn(next(iter(by_cat.values())))
        else:
            names = "; ".join(f"{t.lower()}: {kn(by_cat.get(c, ()))}" for c, t in categories(sid))
        self.log(f"[{sec.title}] к загрузке ({names}): {total} в {len(plan)} папках")
        if skipped_login:
            self.log(f"   пропущено {skipped_login} ссылок на страницы VK — загрузите cookie для входа")

        st = {"got": {k: 0 for k in FILE_KINDS}, "bytes": 0, "dead": 0, "temp": 0,
              "parse": 0, "pages": 0, "done": 0, "total": total}
        t0 = time.time()
        with open(err_path, "w", encoding="utf-8") as err_log:
            err_log.write("папка\tвид\tтип ошибки\tошибка\tссылка\n")
            for d, sc, wanted, state, todo in plan:
                if self.stop.is_set():
                    break
                self._download_scope(d, sc, state, todo, err_log, st)
                state.save()
                self._rewrite_scope(cache, d, sc, wanted, state, st)
            self.save_cache(sid, cache)

        got = ", ".join(f"{KINDS[k]['title'].lower()} {v}" for k, v in st["got"].items() if v) or "ничего"
        verdict = "Прервано" if self.abort_reason else "Остановлено" if self.stop.is_set() else "Готово"
        self.log(f"[{sec.title}] {verdict} за {time.time() - t0:.0f} с. Скачано: {got} "
                 f"({fmt_size(st['bytes'])}); временных ошибок: {st['temp']}; "
                 f"стали недоступны: {st['dead']}; не распознано: {st['parse']}; "
                 f"обновлено страниц: {st['pages']}")
        if self.abort_reason:
            self.log("   ПРИЧИНА: " + self.abort_reason)
        if st["temp"] or st["dead"] or st["parse"]:
            self.log(f"   подробности: {err_path}")
        if st["parse"]:
            self.log(f"   неразобранные страницы VK сохранены в {self.work / 'debug'}")

    def _fetch_one(self, u, k, label, msg_info=None):
        """Скачивает один файл во временный файл. -> (tmp, ctype, size, url_for_name, hint)."""
        tgt = target(k)
        opener = self.session.opener if self.session else None

        def attempt(dest):
            if needs_login(k):
                res = self._resolve_link(u, k, label, msg_info)
                if res.resp is not None:
                    resp, head, src = res.resp, res.head, u
                else:
                    resp, head, src = open_url(res.url, opener), None, res.url
                hint = label or res.name
            else:
                resp, head, src, hint = open_url(u, opener), None, u, None
            sname = server_filename(resp)
            tmp, ctype, size = stream_to_temp(resp, tgt, dest, self.stop, head)
            return tmp, ctype, size, src, (hint or sname)

        return lambda dest: retrying(lambda: attempt(dest), self.stop)

    # ------------------------------------------------------------ вложения через сообщения
    def prefetch_messages(self, sc, links):
        """Для ссылок на страницы VK из переписки получает вложения их сообщений через API.
        -> {url: (kind, obj) | ("dead", причина)}; ссылки без ответа — не попадают."""
        if not self.api:
            return {}
        need = {}
        for u, k in links:
            mid = sc["mids"].get(u)
            if mid is None:
                m = re.search(r"mail(\d+)", u)
                mid = int(m.group(1)) if m else None
            if needs_login(k) and mid is not None:
                key = mid if isinstance(mid, str) else int(mid)
                need.setdefault(key, []).append((u, k))
        if not need:
            return {}
        if self.api_failures >= 2:            # API лежит весь запуск — не тратим время на повторы
            return {u: ("retry", "API VK недоступен в этом запуске") for items in need.values() for u, _ in items}
        try:
            msgs = {}
            ids = sorted(x for x in need if isinstance(x, int))
            if ids:
                msgs.update(self.api.message_attachments(ids))
            posts = [x[2:] for x in need if isinstance(x, str) and x.startswith("w:")]
            if posts:
                msgs.update({"w:" + p: v for p, v in self.api.post_attachments(posts).items()})
            comms = [x[2:] for x in need if isinstance(x, str) and x.startswith("c:")]
            if comms:
                msgs.update({"c:" + c: v for c, v in self.api.comment_attachments(comms).items()})
        except (LoginRequired, CaptchaRequired):
            raise
        except Exception as e:
            # временный сбой API: не идём на страницы VK (для вложений сообщений они всегда
            # отвечают «нет доступа» — ссылки ошибочно стали бы «недоступными»), а повторим позже
            self.api_failures += 1
            self.log(f"   не удалось получить сообщения/записи через API: {e}")
            if self.api_failures == 2:
                self.log("   API VK не отвечает — до конца запуска вложения через API пропускаются "
                         "(останутся во «временных ошибках», повторите позже)")
            return {u: ("retry", f"API недоступен: {e}") for items in need.values() for u, _ in items}
        self.api_failures = 0
        out = {}
        for mid, items in need.items():
            atts = msgs.get(mid)
            byk, byid, seen = {}, {}, []
            for a in atts or []:
                t, key, obj = att_key(a)
                if t:
                    byk[(t, key)] = obj
                    byid.setdefault((t, key.split("_")[-1]), obj)
                seen.append(f"{a.get('type')}{':' + key if key else ''}")
            what = "сообщение" if isinstance(mid, int) else "запись" if mid.startswith("w:") else "комментарий"
            for u, k in items:
                if atts is None:
                    out[u] = ("dead", f"{what} удалено или недоступно")
                    continue
                m = re.search(r"(photo|doc|video)(-?\d+_\d+)", re.sub(r"%2F", "/", u))
                obj = None
                if m:
                    obj = byk.get((m.group(1), m.group(2))) or byid.get((m.group(1), m.group(2).split("_")[-1]))
                if obj:
                    out[u] = (m.group(1), obj)
                else:
                    have = ", ".join(seen) if seen else "вложений нет"
                    out[u] = ("dead", f"вложение удалено ({what}: {have})")
        return out

    def _resolve_link(self, u, k, label, msg_info):
        if msg_info:
            t, obj = msg_info
            if t == "dead":
                raise PermanentError(obj)
            if t == "retry":
                raise TemporaryError(obj)
            if t == "doc" and obj.get("url"):
                name = obj.get("title") or label
                if name and obj.get("ext") and not name.lower().endswith("." + obj["ext"].lower()):
                    name += "." + obj["ext"]
                if re.match(r"https?://(m\.)?vk\.(ru|com)/doc", obj["url"]):
                    # API дал ссылку на страницу документа (с ключом) — разбираем её
                    from .resolvers import resolve_doc
                    res = resolve_doc(self.session, obj["url"], self.debug, label or name)
                    res.note = "страница документа из API"
                    return res
                return Resolved(url=obj["url"], name=label or name, note="из API")
            if t == "photo":
                url = best_photo_url(obj)
                if url:
                    return Resolved(url=url, note="из сообщения (API)")
            if t == "video":
                url, q = best_video_url(obj, self.max_video_q)
                if not url and obj.get("access_key"):
                    try:
                        r = self.api.call("video.get", videos=f"{obj['owner_id']}_{obj['id']}_{obj['access_key']}")
                        items = r.get("items") or []
                        if items:
                            url, q = best_video_url(items[0], self.max_video_q)
                            if not url and items[0].get("platform"):
                                raise PermanentError(f"внешнее видео ({items[0]['platform']}) — не хранится в VK")
                    except ApiError:
                        pass
                if url:
                    return Resolved(url=url, name=f"video{obj['owner_id']}_{obj['id']}_{q}p.mp4",
                                    note=f"{q}p из сообщения (API)")
        if k == "video_page" and self.api and not msg_info:
            m = re.search(r"video(-?\d+_\d+)", u)
            if m:
                try:
                    r = self.api.call("video.get", videos=m.group(1))
                    items = r.get("items") or []
                    if items:
                        url, q = best_video_url(items[0], self.max_video_q)
                        if url:
                            return Resolved(url=url, name=f"video{m.group(1)}_{q}p.mp4", note=f"{q}p video.get (API)")
                        if items[0].get("platform"):
                            raise PermanentError(f"внешнее видео ({items[0]['platform']}) — не хранится в VK")
                except ApiError:
                    pass
        if k == "photo_page" and self.api and not msg_info:
            m = re.search(r"photo(-?\d+_\d+)", u)
            if m:
                try:
                    items = self.api.call("photos.getById", photos=m.group(1))
                    items = items.get("items", []) if isinstance(items, dict) else items
                    url = best_photo_url(items[0]) if items else None
                    if url:
                        return Resolved(url=url, note="photos.getById (API)")
                    if not items:
                        raise PermanentError("фото удалено или недоступно (API вернул пусто)")
                except ApiError as e:
                    if e.code in (15, 30, 200):
                        raise PermanentError(f"нет доступа к фото ({e})")
        return resolve(self.session, u, k, self.debug, label, self.max_video_q)

    def _download_scope(self, d, sc, state, todo, err_log, st):
        if not todo:
            return
        try:
            msg_res = self.prefetch_messages(sc, todo)
        except (LoginRequired, CaptchaRequired) as e:
            self.abort_reason = str(e)
            self.log("ОСТАНОВКА: " + str(e))
            self.stop.set()
            return
        used = {k: {n.lower() for n in idx.values()} for k, idx in state.index.items()}
        lock = threading.Lock()

        def job(u, k):
            tgt = target(k)
            dest = state.folder(tgt)
            tmp, ctype, size, src, hint = self._fetch_one(u, k, sc["labels"].get(u), msg_res.get(u))(dest)
            with lock:
                name = state.index[tgt].get(u) or pick_name(src, used[tgt], tgt, hint, ctype)
                used[tgt].add(name.lower())
                os.replace(tmp, dest / name)
            return tgt, name, size

        pool = ThreadPoolExecutor(max_workers=self.workers)
        futs = {pool.submit(job, u, k): (u, k) for u, k in todo}
        try:
            for fut in as_completed(futs):
                u, k = futs[fut]
                if fut.cancelled():
                    continue
                try:
                    tgt, name, size = fut.result()
                    state.index[tgt][u] = name
                    state.failed.pop(u, None)
                    state.retry.pop(u, None)
                    st["got"][tgt] += 1
                    st["bytes"] += size
                except Stopped:
                    continue
                except (LoginRequired, CaptchaRequired) as e:
                    if not self.abort_reason:
                        self.abort_reason = str(e)
                        self.log("ОСТАНОВКА: " + str(e))
                    self.stop.set()
                    continue
                except ParseError as e:
                    st["parse"] += 1
                    err_log.write(f"{d}\t{k}\tне распознано\t{e}\t{u}\n")
                except PermanentError as e:
                    state.failed[u] = str(e)
                    state.retry.pop(u, None)
                    st["dead"] += 1
                    err_log.write(f"{d}\t{k}\tокончательно\t{e}\t{u}\n")
                except Exception as e:
                    if self.stop.is_set():
                        continue
                    state.retry[u] = state.retry.get(u, 0) + 1
                    if state.retry[u] >= MAX_TEMP_RUNS:
                        state.failed[u] = f"{e} ({state.retry.pop(u)} запуска подряд)"
                        st["dead"] += 1
                        err_log.write(f"{d}\t{k}\tокончательно\t{state.failed[u]}\t{u}\n")
                    else:
                        st["temp"] += 1
                        err_log.write(f"{d}\t{k}\tвременно\t{e}\t{u}\n")
                st["done"] += 1
                if st["done"] % 5 == 0 or st["done"] == st["total"]:
                    self.progress(st["done"], st["total"], f"{d}  ·  {fmt_size(st['bytes'])}")
                if st["done"] % 100 == 0:
                    state.save()
                    err_log.flush()
                if self.stop.is_set():
                    for f in futs:
                        f.cancel()
        finally:
            pool.shutdown(wait=True)

    def _rewrite_scope(self, cache, d, sc, wanted, state, st):
        resolved = {}
        for u, k in wanted.items():
            tgt = target(k)
            n = state.local_name(u, tgt)
            if n:
                resolved[u] = (tgt, f"{KINDS[tgt]['folder']}/{n}", sc["labels"].get(u, ""))
        if not resolved:
            return
        for rel in sc["files"]:
            e = cache["files"].get(rel)
            if not e or not any(u in resolved for u in e["r"]):
                continue
            p = self.root / rel
            raw = p.read_bytes()
            new, n = htmlmedia.rewrite(raw, resolved)
            if not n:
                continue
            b = self.root / BACKUP_DIR / rel
            if not b.exists():
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, b)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(new)
            os.replace(tmp, p)
            cache["files"][rel] = self._entry(p, new, rel)
            st["pages"] += 1

    def rebuild(self, sid):
        """Пересобрать страницы раздела: вернуть исходные HTML из _backup_html и заново
        встроить все уже скачанные файлы (ничего не скачивает)."""
        sec = SECTIONS_BY_ID[sid]
        cache = self.load_cache(sid, any_version=True)
        restored = 0
        for rel in list(cache["files"]):
            b = self.root / BACKUP_DIR / rel
            p = self.root / rel
            if b.exists() and p.exists() and b.read_bytes() != p.read_bytes():
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_bytes(b.read_bytes())
                os.replace(tmp, p)
                restored += 1
        cache = self.scan(sid)
        st = {"pages": 0}
        for d, sc in self._scopes(cache).items():
            if self.stop.is_set():
                break
            self._rewrite_scope(cache, d, sc, sc["remote"], ScopeState(self.root / d), st)
        self.save_cache(sid, cache)
        self.log(f"[{sec.title}] восстановлено из копий: {restored}, страниц с файлами: {st['pages']}")

    # ------------------------------------------------------------ проверка на примерах
    def probe(self, per_kind=3):
        """Пробует по несколько ссылок на страницы VK каждого вида, ничего не сохраняя
        в архив. Отчёт и неразобранные страницы — в _vksaver/debug/."""
        if not self.session:
            self.log("Сначала загрузите cookie для входа в VK.")
            return
        dbg_dir = self.work / "debug"
        self.debug = DebugSaver(dbg_dir, limit=per_kind)
        self.api = VKApi(self.session)
        try:
            self.api.ensure_token()
            self.log(f"Токен веб-версии VK получен (API: {self.api.domain}).")
        except (LoginRequired, CaptchaRequired) as e:
            self.log("ОСТАНОВКА: " + str(e))
            return
        except Exception as e:
            self.log(f"Токен веб-версии получить не удалось ({e}) — вложения сообщений будут "
                     f"искаться только через страницы VK.")
            self.api = None
        for sid in ("wall", "profile", "likes"):      # небольшие разделы — освежить кэш
            self.scan(sid)
        samples = {"photo_page": [], "doc_page": [], "video_page": []}
        per_sec = {}
        for s in SECTIONS:
            cache = self.load_cache(s.id, any_version=True)
            for d, sc in self._scopes(cache).items():
                if not sc["mids"] and any(k.endswith("_page") for k in sc["remote"].values()):
                    for rel in sc["files"][:20]:          # старый кэш без ID сообщений — дочитать
                        try:
                            sc["mids"].update(htmlmedia.extract((self.root / rel).read_bytes())[3])
                        except OSError:
                            pass
                for u, k in sc["remote"].items():
                    if k in samples and per_sec.get((k, s.id), 0) < per_kind * 2:
                        per_sec[(k, s.id)] = per_sec.get((k, s.id), 0) + 1
                        samples[k].append((s.id, d, u, sc["labels"].get(u), sc))
        report = []
        n_total = sum(min(per_kind, len(v)) for v in samples.values())
        done = 0
        for k, items in samples.items():
            # сначала по одному примеру из разных разделов, потом из разных папок
            picked, seen_s, seen_d = [], set(), set()
            for it in items:
                if it[0] not in seen_s:
                    picked.append(it)
                    seen_s.add(it[0])
                    seen_d.add(it[1])
            for it in items:
                if it not in picked and it[1] not in seen_d:
                    picked.append(it)
                    seen_d.add(it[1])
            picked += [it for it in items if it not in picked]
            picked = picked[:per_kind]
            if not picked:
                report.append(f"{k}: примеров нет (сначала просканируйте разделы)")
            for sid, d, u, label, sc in picked:
                if self.stop.is_set():
                    break
                done += 1
                self.progress(done, n_total, f"{KINDS[k]['title']}: {u}")
                try:
                    info = self.prefetch_messages(sc, [(u, k)]).get(u)
                    res = self._resolve_link(u, k, label, info)
                    if res.resp is not None:
                        resp, head = res.resp, res.head
                        what = "файл отдан сразу"
                    else:
                        resp = open_url(res.url, self.session.opener)
                        head = resp.read(65536)
                        what = f"ссылка {res.url.split('?')[0][:90]}"
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0]
                    size = resp.headers.get("Content-Length") or "?"
                    resp.close()
                    from .downloader import looks_like
                    ok = looks_like(target(k), head, ctype)
                    line = (f"{'OK ' if ok else 'НЕ ТО'} {k} {u}\n     -> {what}; {ctype}, "
                            f"{size} байт; {res.note}; начало {head[:12]!r}")
                except (LoginRequired, CaptchaRequired) as e:
                    report.append(f"СТОП {k} {u}: {e}")
                    self.log("ОСТАНОВКА: " + str(e))
                    break
                except Exception as e:
                    line = f"ОШИБКА {k} {u}\n     -> {type(e).__name__}: {e}"
                report.append(line)
                self.log(line)
        dbg_dir.mkdir(parents=True, exist_ok=True)
        rp = dbg_dir / f"probe_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        rp.write_text("\n".join(report) + "\n", encoding="utf-8")
        self.log(f"Отчёт проверки: {rp}")
