# -*- coding: utf-8 -*-
"""Запуск:  python -m vksaver                     — окно программы
           python -m vksaver scan messages        — сканирование
           python -m vksaver download messages --kinds photo,voice,doc
           python -m vksaver cookies ФАЙЛ.json    — загрузить cookie для входа в VK
           python -m vksaver login                — проверить вход
           python -m vksaver probe                — проверить загрузку на нескольких примерах
"""
import argparse
import sys
import threading
from pathlib import Path

from .sections import SECTIONS, SECTIONS_BY_ID, SECTION_ALIASES, CHAT_CATEGORIES


def gui_missing_message(err):
    return (f"Не удалось открыть окно программы: {err}\n"
            "Похоже, в этом Python нет модуля tkinter.\n"
            "  Debian/Ubuntu:  sudo apt install python3-tk\n"
            "  Fedora:         sudo dnf install python3-tkinter\n"
            "  Arch:           sudo pacman -S tk\n"
            "  macOS (Homebrew): brew install python-tk\n"
            "Командная строка работает и без него: python3 -m vksaver --help")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        try:
            from .gui import run
        except ImportError as e:
            sys.exit(gui_missing_message(e))
        return run()

    from .config import load_settings
    from .engine import Engine, STAT_COLUMNS
    from .common import KINDS, FILE_KINDS
    from . import vkauth

    ap = argparse.ArgumentParser(prog="python -m vksaver")
    ap.add_argument("command", choices=["scan", "download", "stats", "cookies", "login", "probe", "logout", "rebuild"])
    ap.add_argument("arg", nargs="?", default="all",
                    help="раздел (" + ", ".join(s.id for s in SECTIONS) + ", all) или файл cookie")
    ap.add_argument("--archive", help="папка архива VK (по умолчанию из настроек)")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--kinds", help="что качать: " + ",".join(FILE_KINDS) + " (по умолчанию всё)")
    ap.add_argument("--chats", help="для сообщений: personal,group,community (по умолчанию все)")
    ap.add_argument("--video-quality", type=int, help="максимальное качество видео (360/480/720/1080)")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--full", action="store_true", help="сканировать всё заново, без кэша")
    a = ap.parse_args(argv)

    settings = load_settings()
    root = a.archive or settings.get("archive")

    if a.command == "cookies":
        try:
            cookies = vkauth.vk_only(vkauth.parse_cookies(Path(a.arg).read_text(encoding="utf-8-sig")))
        except (OSError, vkauth.CookieFormatError) as e:
            sys.exit(f"Не удалось загрузить cookie: {e}")
        vkauth.save_cookies(cookies)
        print("Cookie сохранены:", vkauth.summarize(cookies))
        a.command = "login"
    if a.command == "logout":
        vkauth.forget()
        print("Cookie удалены.")
        return
    if not root:
        sys.exit("Укажите папку архива: --archive ПУТЬ")
    session = vkauth.make_session(root)
    if a.command == "login":
        if not session:
            sys.exit("Cookie не загружены: python -m vksaver cookies ФАЙЛ.json")
        ok, msg = session.check()
        print(msg)
        return

    def progress(done, total, text):
        if total:
            sys.stdout.write(f"\r  {done}/{total}  {text[:70]:<70}")
            sys.stdout.flush()

    def log(text):
        sys.stdout.write("\r" + " " * 100 + "\r")
        print(text, flush=True)

    stop = threading.Event()
    eng = Engine(root, log=log, progress=progress, stop=stop, session=session,
                 workers=a.workers or settings.get("workers", 6),
                 max_video_q=a.video_quality or settings.get("max_video_quality", 720))
    if a.command == "probe":
        eng.probe()
        return

    if a.arg == "all":
        sids = [s.id for s in SECTIONS]
    else:
        sids = SECTION_ALIASES.get(a.arg, [a.arg])
    for sid in sids:
        if sid not in SECTIONS_BY_ID:
            sys.exit(f"Нет такого раздела: {sid}. Есть: " + ", ".join(s.id for s in SECTIONS))
    kinds = [k.strip() for k in a.kinds.split(",")] if a.kinds else None
    bad = [k for k in (kinds or []) if k not in FILE_KINDS]
    if bad:
        sys.exit(f"Неизвестные виды: {', '.join(bad)}. Есть: {', '.join(FILE_KINDS)}")
    msg_kinds = kinds
    if a.chats:
        chosen = {c.strip() for c in a.chats.split(",")}
        msg_kinds = {c: (kinds or FILE_KINDS) if c in chosen else [] for c, _ in CHAT_CATEGORIES}
    try:
        for sid in sids:
            if a.command == "scan":
                eng.scan(sid, full=a.full)
            elif a.command == "rebuild":
                eng.rebuild(sid)
            elif a.command == "download":
                eng.download(sid, kinds=msg_kinds if sid == "messages" else kinds,
                             retry_failed=a.retry_failed)
                if eng.abort_reason:
                    break
            rows, cache = eng.stats(sid)
            print(f"\n== {SECTIONS_BY_ID[sid].title} (сканирование: {cache.get('scanned_at') or 'не было'})")
            print(f"{'':12}" + "".join(f"{t:>16}" for _, t in STAT_COLUMNS))
            for k in FILE_KINDS:
                r = rows[k]
                if r["total"]:
                    print(f"{KINDS[k]['title']:12}" + "".join(f"{r[c]:>16}" for c, _ in STAT_COLUMNS))
    except KeyboardInterrupt:
        stop.set()
        print("\nПрервано.")


if __name__ == "__main__":
    main()
