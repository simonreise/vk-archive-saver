# -*- coding: utf-8 -*-
"""Окно программы (tkinter — входит в стандартный Python для Windows)."""
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

from . import __version__
from .common import KINDS, FILE_KINDS, WORK_DIR
from . import vkauth
from .config import load_settings, save_settings
from .engine import Engine, STAT_COLUMNS
from .sections import SECTIONS, categories, ALL

LOGIN_HINT = ("«Через вход в VK» — в выгрузке есть только ссылка на страницу VK. Такие файлы "
              "скачиваются, если загружены cookie вашего браузера (кнопка «Загрузить cookie…»). "
              "Видео занимают много места — включайте их отдельно.")
DEFAULT_KINDS = {"photo": True, "voice": True, "doc": True, "video": False}


def open_path(p):
    p = str(p)
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)                     # noqa: на Windows есть
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:
        messagebox.showerror("Не удалось открыть", f"{p}\n\n{e}")


class SectionTab:
    def __init__(self, app, nb, section):
        self.app = app
        self.section = section
        self.frame = ttk.Frame(nb, padding=10)
        nb.add(self.frame, text=f"  {section.title}  ")

        ttk.Label(self.frame, text=section.note, foreground="#555").pack(anchor="w")
        self.info = ttk.Label(self.frame, text="Сканирование ещё не выполнялось.")
        self.info.pack(anchor="w", pady=(2, 8))

        cols = ["kind"] + [c for c, _ in STAT_COLUMNS]
        self.tree = ttk.Treeview(self.frame, columns=cols, show="headings", height=8,
                                 selectmode="none")
        self.tree.heading("kind", text="Вид")
        self.tree.column("kind", width=200, minwidth=150, anchor="w")
        for c, title in STAT_COLUMNS:
            self.tree.heading(c, text=title)
            self.tree.column(c, width=140 if c == "login" else 112, minwidth=80, anchor="e")
        self.tree.tag_configure("total", font=app.bold)
        self.tree.tag_configure("login", foreground="#8a6d00")
        self.tree.tag_configure("empty", foreground="#888")
        self.tree.pack(fill="x")

        saved = app.settings.get("kinds", {}).get(section.id, {})
        if saved and all(isinstance(v, bool) for v in saved.values()):   # старый формат
            saved = {c: saved for c, _ in categories(section.id)}
        elif ALL in saved and ALL not in dict(categories(section.id)):  # раньше без категорий
            saved = {c: saved[ALL] for c, _ in categories(section.id)}
        self.kinds, self.cat_info = {}, {}
        cats = categories(section.id)
        for i, (cat, ctitle) in enumerate(cats):
            kf = ttk.Frame(self.frame)
            kf.pack(fill="x", pady=(10 if i == 0 else 2, 0))
            ttk.Label(kf, text="Скачивать:" if cat == ALL else f"{ctitle}:", width=15).pack(side="left")
            self.kinds[cat] = {}
            for k in section.kinds:
                default = DEFAULT_KINDS[k] and k not in section.default_off
                v = tk.BooleanVar(value=saved.get(cat, {}).get(k, default))
                self.kinds[cat][k] = v
                ttk.Checkbutton(kf, text=KINDS[k]["title"], variable=v).pack(side="left", padx=(8, 0))
            if cat != ALL:
                lbl = ttk.Label(kf, text="", foreground="#666")
                lbl.pack(side="left", padx=(16, 0))
                self.cat_info[cat] = lbl

        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", pady=(8, 0))
        self.btn_scan = ttk.Button(bar, text="Сканировать", command=self.scan)
        self.btn_scan.pack(side="left")
        self.btn_dl = ttk.Button(bar, text="Скачать", command=self.download)
        self.btn_dl.pack(side="left", padx=6)
        self.retry = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="ещё раз попробовать недоступные", variable=self.retry).pack(side="left", padx=6)
        ttk.Button(bar, text="Журнал ошибок", command=self.open_errors).pack(side="right")
        self.btn_rebuild = ttk.Button(bar, text="Обновить страницы", command=self.rebuild)
        self.btn_rebuild.pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Открыть папку", command=self.open_folder).pack(side="right", padx=6)
        self.show_rows(None, None)

    # --- действия
    def scan(self):
        self.app.start(self.section, "scan")

    def download(self):
        kinds = {cat: [k for k, v in kv.items() if v.get()] for cat, kv in self.kinds.items()}
        if not any(kinds.values()):
            messagebox.showinfo("Скачать", "Отметьте, что скачивать: фото, голосовые, документы или видео.")
            return
        self.app.start(self.section, "download", retry=self.retry.get(), kinds=kinds)

    def rebuild(self):
        if messagebox.askyesno("Обновить страницы",
                               "Вернуть исходные HTML этого раздела из _backup_html и заново встроить "
                               "все уже скачанные файлы?\nНичего не скачивается, займёт немного времени."):
            self.app.start(self.section, "rebuild")

    def open_errors(self):
        p = Path(self.app.archive.get()) / WORK_DIR / f"errors_{self.section.id}.log"
        if p.exists():
            open_path(p)
        else:
            messagebox.showinfo("Журнал ошибок", "Ошибок пока не было — журнал появится после загрузки.")

    def open_folder(self):
        root = Path(self.app.archive.get())
        first = self.section.patterns[0].split("/")[0]
        open_path(root / first if (root / first).exists() else root)

    # --- отображение
    def show_rows(self, rows, cache):
        self.tree.delete(*self.tree.get_children())
        cats = (rows or {}).get("_cats", {})
        for cat, lbl in self.cat_info.items():
            left = cats.get(cat, {})
            parts = [f"{KINDS[k]['title'].lower()} {_n(left[k])}" for k in FILE_KINDS if left.get(k)]
            lbl.config(text=("осталось: " + ", ".join(parts)) if parts else ("всё скачано" if cats else ""))
        if not rows or not cache or not cache.get("files"):
            self.tree.insert("", "end", values=["Нажмите «Сканировать»"] + [""] * len(STAT_COLUMNS),
                             tags=("empty",))
            self.info.config(text="Сканирование ещё не выполнялось.")
            return
        total = {c: 0 for c, _ in STAT_COLUMNS}
        shown = 0
        for k in FILE_KINDS:
            r = rows[k]
            if not r["total"]:
                continue
            shown += 1
            for c in total:
                total[c] += r[c]
            tags = ()
            self.tree.insert("", "end", values=[KINDS[k]["title"]] + [_n(r[c]) for c, _ in STAT_COLUMNS],
                             tags=tags)
        if not shown:
            self.tree.insert("", "end", values=["Медиа-ссылок не найдено"] + [""] * len(STAT_COLUMNS),
                             tags=("empty",))
        else:
            self.tree.insert("", "end", values=["Итого"] + [_n(total[c]) for c, _ in STAT_COLUMNS],
                             tags=("total",))
        self.info.config(text=f"Последнее сканирование: {cache.get('scanned_at') or 'не завершено'}"
                              f"   ·   HTML-файлов: {len(cache['files']):,}".replace(",", " "))


def _n(v):
    return f"{v:,}".replace(",", " ") if v else "—"


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.worker = None

        root.title(f"VK Archive Saver {__version__}")
        root.geometry("1080x760")
        root.minsize(980, 640)
        style = ttk.Style()
        names = style.theme_names()
        if sys.platform.startswith("win") and "vista" in names:
            style.theme_use("vista")
        elif sys.platform != "darwin" and "clam" in names:     # на macOS оставляем родную aqua
            style.theme_use("clam")
        import tkinter.font as tkfont
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        style.configure("Treeview", rowheight=24)

        # --- верхняя панель
        top = ttk.Frame(root, padding=(10, 10, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Архив VK:").pack(side="left")
        self.archive = tk.StringVar(value=self.settings.get("archive", ""))
        ttk.Entry(top, textvariable=self.archive).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Выбрать…", command=self.choose_archive).pack(side="left")
        ttk.Label(top, text="   Потоков:").pack(side="left")
        self.workers = tk.IntVar(value=int(self.settings.get("workers", 6)))
        ttk.Spinbox(top, from_=1, to=16, width=4, textvariable=self.workers).pack(side="left", padx=(4, 0))
        ttk.Label(top, text="   Видео до:").pack(side="left")
        self.vq = tk.StringVar(value=f"{self.settings.get('max_video_quality', 720)}p")
        ttk.Combobox(top, textvariable=self.vq, width=6, state="readonly",
                     values=["360p", "480p", "720p", "1080p", "2160p"]).pack(side="left", padx=(4, 0))

        # --- вход в VK
        lg = ttk.Frame(root, padding=(10, 8, 10, 0))
        lg.pack(fill="x")
        ttk.Label(lg, text="Вход в VK:").pack(side="left")
        self.login_btns = []
        for text, cmd in reversed((("Загрузить cookie…", self.load_cookies_file),
                                   ("Из буфера", self.load_cookies_clipboard),
                                   ("Проверить вход", self.check_login),
                                   ("Тест на примерах", self.probe),
                                   ("Забыть", self.forget_cookies))):
            b = ttk.Button(lg, text=text, command=cmd)
            b.pack(side="right", padx=(4, 0))
            self.login_btns.append(b)
        self.login_lbl = ttk.Label(lg, text="")
        self.login_lbl.pack(side="left", padx=6, fill="x", expand=True)
        self.update_login_label()

        # --- вкладки
        nb = ttk.Notebook(root)
        nb.pack(fill="x", padx=10, pady=10)
        self.tabs = {s.id: SectionTab(self, nb, s) for s in SECTIONS}

        ttk.Label(root, text=LOGIN_HINT, foreground="#8a6d00", wraplength=960,
                  justify="left").pack(fill="x", padx=12)

        # --- прогресс
        pf = ttk.Frame(root, padding=(10, 8, 10, 0))
        pf.pack(fill="x")
        self.status = ttk.Label(pf, text="Готово.")
        self.status.pack(anchor="w")
        row = ttk.Frame(pf)
        row.pack(fill="x", pady=(4, 0))
        self.pbar = ttk.Progressbar(row, mode="determinate")
        self.pbar.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(row, text="Остановить", command=self.request_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))

        # --- журнал
        lf = ttk.Frame(root, padding=10)
        lf.pack(fill="both", expand=True)
        self.logbox = tk.Text(lf, height=10, wrap="word", state="disabled", font="TkFixedFont")
        sb = ttk.Scrollbar(lf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        self.write_log("Готово к работе. Выберите вкладку и нажмите «Сканировать».")
        self.refresh_all()

    # ------------------------------------------------------------ helpers
    def write_log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def choose_archive(self):
        d = filedialog.askdirectory(title="Папка архива VK (где index.html и messages)",
                                    initialdir=self.archive.get() or None)
        if d:
            self.archive.set(d)
            self.save()
            self.refresh_all()

    def save(self):
        self.settings["archive"] = self.archive.get()
        try:
            self.settings["workers"] = max(1, min(16, int(self.workers.get())))
        except (tk.TclError, ValueError):
            pass
        try:
            self.settings["max_video_quality"] = int(self.vq.get().rstrip("p"))
        except ValueError:
            pass
        self.settings["kinds"] = {sid: {cat: {k: v.get() for k, v in kv.items()}
                                        for cat, kv in t.kinds.items()}
                                  for sid, t in self.tabs.items()} if hasattr(self, "tabs") else {}
        save_settings(self.settings)

    def check_archive(self):
        p = Path(self.archive.get())
        if not (p / "index.html").exists():
            messagebox.showwarning("Архив не найден",
                                   "Укажите папку выгрузки VK — ту, где лежат index.html и папка messages.")
            return None
        return p

    def set_busy(self, busy):
        st = "disabled" if busy else "normal"
        for t in self.tabs.values():
            t.btn_scan.configure(state=st)
            t.btn_dl.configure(state=st)
            t.btn_rebuild.configure(state=st)
        for b in self.login_btns:
            b.configure(state=st)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    # ------------------------------------------------------------ фоновая работа
    # ------------------------------------------------------------ вход в VK
    def update_login_label(self, text=None):
        if text is None:
            c = vkauth.load_saved()
            text = ("cookie загружены: " + vkauth.summarize(c)) if c else "cookie не загружены"
        self.login_lbl.configure(text=text)

    def _import_cookies(self, text, source):
        try:
            cookies = vkauth.vk_only(vkauth.parse_cookies(text))
        except vkauth.CookieFormatError as e:
            messagebox.showerror("Cookie", f"Не удалось прочитать cookie ({source}): {e}")
            return
        if not cookies:
            messagebox.showerror("Cookie", "Среди cookie нет ни одной для vk.ru / vk.com.")
            return
        vkauth.save_cookies(cookies)
        self.write_log(f"Cookie загружены ({source}): {vkauth.summarize(cookies)}. "
                       f"Хранятся в {vkauth.COOKIES_PATH}")
        self.update_login_label()
        self.check_login()

    def load_cookies_file(self):
        p = filedialog.askopenfilename(title="Cookie для vk.ru (JSON из Cookie-Editor или cookies.txt)",
                                       filetypes=[("Cookie", "*.json *.txt"), ("Все файлы", "*.*")])
        if p:
            try:
                text = Path(p).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as e:
                messagebox.showerror("Cookie", str(e))
                return
            self._import_cookies(text, Path(p).name)

    def load_cookies_clipboard(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("Cookie", "Буфер обмена пуст. В Cookie-Editor нажмите Export → JSON.")
            return
        self._import_cookies(text, "из буфера обмена")

    def forget_cookies(self):
        if messagebox.askyesno("Забыть вход", "Удалить сохранённые cookie VK с этого компьютера?"):
            vkauth.forget()
            self.update_login_label()
            self.write_log("Cookie удалены.")

    def check_login(self):
        if not vkauth.load_saved():
            messagebox.showinfo("Вход в VK", "Сначала загрузите cookie.")
            return
        self.start(None, "login")

    def probe(self):
        if not vkauth.load_saved():
            messagebox.showinfo("Тест", "Сначала загрузите cookie.")
            return
        self.start(None, "probe")

    # ------------------------------------------------------------ фоновая работа
    def start(self, section, op, retry=False, kinds=None):
        if self.worker and self.worker.is_alive():
            return
        root = self.check_archive()
        if not root:
            return
        self.save()
        self.stop.clear()
        self.set_busy(True)
        self.pbar.configure(mode="indeterminate")
        self.pbar.start(12)
        what = {"scan": "Сканирование", "download": "Загрузка", "login": "Проверка входа",
                "rebuild": "Обновление страниц",
                "probe": "Тест на примерах"}[op]
        self.status.configure(text=f"{what}" + (f": {section.title}…" if section else "…"))
        self.worker = threading.Thread(target=self._run, daemon=True,
                                       args=(root, section.id if section else None, op, retry, kinds))
        self.worker.start()

    def _engine(self, root):
        return Engine(root, workers=self.settings.get("workers", 6), stop=self.stop,
                      session=vkauth.make_session(root),
                      max_video_q=self.settings.get("max_video_quality", 720),
                      log=lambda t: self.q.put(("log", t)),
                      progress=lambda d, n, t: self.q.put(("progress", d, n, t)))

    def _run(self, root, sid, op, retry, kinds):
        abort = None
        try:
            eng = self._engine(root)
            if op == "login":
                ok, msg = eng.session.check()
                self.q.put(("log", "Проверка входа: " + msg))
                self.q.put(("login", ("✔ " if ok else "✘ ") + msg))
                return
            if op == "probe":
                eng.probe()
                abort = eng.abort_reason
                return
            if op == "scan":
                eng.scan(sid)
            elif op == "rebuild":
                eng.rebuild(sid)
            else:
                eng.download(sid, kinds=kinds, retry_failed=retry)
                abort = eng.abort_reason
            rows, cache = eng.stats(sid)
            self.q.put(("stats", sid, rows, cache))
        except Exception as e:
            import traceback
            self.q.put(("log", "ОШИБКА: " + "".join(traceback.format_exception_only(type(e), e)).strip()))
            self.q.put(("log", traceback.format_exc()))
        finally:
            self.q.put(("done", abort))

    def refresh_all(self):
        """Показать статистику из кэша прошлых сканирований (в фоне)."""
        root = Path(self.archive.get())
        if not (root / "index.html").exists():
            return

        def work():
            eng = Engine(root)
            for s in SECTIONS:
                try:
                    rows, cache = eng.stats(s.id)
                    self.q.put(("stats", s.id, rows, cache))
                except Exception as e:
                    self.q.put(("log", f"Не удалось прочитать кэш раздела «{s.title}»: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def request_stop(self):
        self.stop.set()
        self.status.configure(text="Останавливаю… (дожидаюсь текущих загрузок)")
        self.btn_stop.configure(state="disabled")

    def poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self.write_log(ev[1])
                elif kind == "progress":
                    _, done, total, text = ev
                    if total:
                        if str(self.pbar.cget("mode")) != "determinate":
                            self.pbar.stop()
                            self.pbar.configure(mode="determinate")
                        self.pbar.configure(maximum=total, value=done)
                        pct = 100.0 * done / total
                        self.status.configure(text=f"{done:,} из {total:,} ({pct:.0f}%)   {text}".replace(",", " "))
                    else:
                        self.status.configure(text=text)
                elif kind == "stats":
                    self.tabs[ev[1]].show_rows(ev[2], ev[3])
                elif kind == "login":
                    self.update_login_label(ev[1])
                elif kind == "done":
                    if ev[1]:
                        messagebox.showwarning("Загрузка прервана", ev[1])
                    self.pbar.stop()
                    self.pbar.configure(mode="determinate", value=0)
                    self.status.configure(text="Остановлено." if self.stop.is_set() else "Готово.")
                    self.set_busy(False)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Выход", "Идёт работа. Остановить и выйти?\n"
                                               "Уже скачанное сохранится."):
                return
            self.stop.set()
            self.status.configure(text="Завершаю…")
            self.root.after(200, self._wait_and_close)
            return
        self.save()
        self.root.destroy()

    def _wait_and_close(self):
        if self.worker and self.worker.is_alive():
            self.root.after(200, self._wait_and_close)
        else:
            self.save()
            self.root.destroy()


def run():
    try:                                   # чёткий шрифт на Windows с масштабированием
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
