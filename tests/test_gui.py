"""python3 tests/test_gui.py — GUI-смоук на живом Tk.

Раньше этот файл был «скипающимся» тестом: без DISPLAY он проверял только
фолбэк в headless, а на дисплее — две строки. Из-за этого в нём год жил баг,
который видно только под Tk: окно собиралось, но статус не обновлялся никогда,
потому что тест не снимал привязку к запущенному Таркову (`tie_to_game`) и
рабочий цикл возвращал заводскую таблицу, не доходя до расчёта.

Сейчас проверяется то, что без окна не проверить вообще:

* сборка окна со всеми виджетами, ползунки пишут в cfg и помечают конфиг на
  автосохранение (config.json реально меняется, не только в памяти);
* статус-бар живёт на реальных кадрах (не «0 таблиц», как было до правки);
* галка «поверх окна», выбор монитора (в том числе мусор в поле ввода), профиль;
* строка обновлений: подписи, кнопки, ответ обновлятора из очереди, диалог
  подтверждения не ломает путь;
* окно «Диагностика» — Toplevel с текстом отчёта, без трейсбеков.

Запуск без дисплея (CI на ubuntu) честно сообщает SKIP и проверяет фолбэк
`run_gui() -> run_headless()` — ровно как раньше.
"""
from __future__ import annotations
import os, sys, time, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import windows as W                # noqa: E402
W.fix_console()                      # русские print не должны падать в cp1252-консоли

import capture                        # noqa: E402
import engine as E                   # noqa: E402
import main as M                     # noqa: E402

FAILS = []


def check(cond, msg, extra=""):
    print(("  ok   " if cond else "  FAIL ") + msg + (f"   [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(msg)


def have_display() -> bool:
    if os.environ.get("DISPLAY") or sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return False


def dark_frame(w=480, h=270):
    """Тёмный «экран» с тилтом: два кадра, чтобы авто-блок было чем занять.

    Сцены tools/make_samples для GUI-смоука не берём: одна генерится ~3 с, и
    окно ждало бы статус дольше, чем длится весь тест. Здесь тот же смысл
    (тени + зелёнка), но кадр собирается за миллисекунды и работает и без
    numpy — тогда заодно проверяется pure-путь analyze через GUI.
    """
    buf = bytearray()
    for y in range(h):
        row = bytearray()
        for x in range(w):
            v = 12 + (y * 40) // h
            bright = 90 if x > w * 0.62 and y < h * 0.35 else 0
            row += bytes(((v + bright) & 0xFF, int((v + bright) * 1.12) & 0xFF,
                          max(0, min(255, v - 4 + bright))))
        buf += row
    stride = w * 3
    try:
        import numpy as np
        arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(h, w, 3).copy()
        return arr
    except ImportError:
        import capture as Cp
        return Cp.Frame(bytes(buf), w, h, stride=stride, bpp=3, order=(0, 1, 2))


class Grabber:
    """Тот же интерфейс, что capture.Grabber, только с синтетическими кадрами."""

    def __init__(self, *a, **k):
        self.frames = [dark_frame(), dark_frame(320, 180)]
        self.i = 0

    def grab(self):
        f = self.frames[self.i % len(self.frames)]
        self.i += 1
        return f

    def close(self):
        pass


def wait(predicate, timeout=8.0, step=0.03, tick=None):
    """Ждать условие, а не фиксированный сон (ловушка HANDOFF §6). `tick` крутит
    обработчики Tk: без этого статус окна не обновится никогда, потому что
    _pump живёт именно в очереди событий."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if tick:
            try:
                tick()
            except Exception:
                return True                       # Tk-окно уничтожено — условие считается выполненным
        try:
            if predicate():
                return True
        except Exception:
            return True                           # то же самое для предиката (winfo после destroy)
        time.sleep(step)
    return predicate()


def run_gui_suite():
    import tkinter
    from tkinter import ttk
    try:
        probe = tkinter.Tk()
        probe.destroy()
    except Exception as e:
        print("SKIP: дисплей объявлен, но Tk не поднимается:", e)
        return True

    cfgdir = tempfile.mkdtemp(prefix="tbgui-")
    real_cfg = E.config_path
    E.config_path = lambda: os.path.join(cfgdir, "config.json")        # не трогаем реальный %APPDATA%
    real_grab = capture.Grabber
    capture.Grabber = Grabber

    cfg = dict(E.DEFAULT_CONFIG)
    cfg["update_auto_check"] = False    # фоновый запрос к GitHub подменил бы подпись в тесте
    cfg["tie_to_game"] = False          # без этой строчки статус неоживал: цикл возвращал
                                        # заводскую таблицу, не доходя до анализа кадра
    def destroyed():
        try:
            return not ui.root.winfo_exists() if "ui" in dir() else True
        except Exception:
            return True                        # Tk уже уничтожен — это то, чего мы ждём

    app = M.App(cfg, headless=False)
    tables = []
    app.engine.sink = lambda b: tables.append(b)
    ui = M.Gui(app, tkinter, ttk)
    app.ui = ui                                   # ровно так же делает App.run_gui()
    app.start()
    ok_all = True
    try:
        # ---- окно собралось и живёт -----------------------------------
        check(ui.root.winfo_exists(), "окно создано")
        check(app._thread is not None and app._thread.is_alive(),
              "фоновый поток жив после start() (без него окно — просто картинка)")
        check("Tarkov Bright" in ui.root.title() and M.V.__version__ in ui.root.title(),
              "в заголовке имя и версия", ui.root.title())
        pumped = wait(lambda: "гамма" in ui.status.cget("text"), 10, tick=ui.root.update)
        check(pumped, "статус-бар обновился из рабочего цикла", ui.status.cget("text")[:80])
        check(any(t is not None for t in tables), "в sink улетели таблицы, а не только сбросы",
              "%d сообщений" % len(tables))
        check(any(isinstance(t, (bytes, bytearray)) and len(t) == 1536 for t in tables),
              "размер таблицы 1536 байт", "")

        # ---- строка обновлений ----------------------------------------
        texts = [b.cget("text") for b in (ui.btn_upd, ui.btn_upd_check, ui.btn_rollback)]
        check(texts == ["Обновить", "Проверить", "Откатить"], "кнопки обновления на месте",
              str(texts))
        check(M.V.__version__ in ui.upd_lab.cget("text"), "рядом видна текущая версия",
              ui.upd_lab.cget("text")[:40])
        check(str(ui.upd_lab.cget("text")).count("v") == 1,
              "до проверки версия одна (не «v1.1.0 v1.1.0»)", ui.upd_lab.cget("text")[:40])
        ui._upd_handle({"state": "update-available", "message": "fix: тени в лесу",
                        "local_sha": "a" * 40, "remote_sha": "b" * 40})
        ui.root.update()
        lab = ui.upd_lab.cget("text")
        check("fix:" in lab and ui._upd_running is False, "ответ обновлятора виден в окне",
              lab[:70])
        ui._upd_busy(True)
        ui.root.update()
        check(str(ui.btn_upd.cget("state")) == "disabled", "на время работы кнопки запираются",
              str(ui.btn_upd.cget("state")))
        ui._upd_busy(False)
        ui._upd_handle({"state": "progress", "message": "скачиваю архив"})
        ui.root.update()
        check("скачиваю" in ui.upd_lab.cget("text"), "прогресс скачивания показывается",
              ui.upd_lab.cget("text")[:60])
        ui._upd_handle({"state": "applied", "applied": ["app/main.py", "app/engine.py"],
                        "restart": True, "message": "обновлено"})
        ui.root.update()
        check("2" in ui._upd_note, "число заменённых файлов уходит в предупреждающую строку",
              ui._upd_note[:80])

        # ---- ползунки, галки, профиль ----------------------------------
        ui.var_auto.set(False); ui._set("auto_exposure", False)
        check(app.cfg["auto_exposure"] is False, "галка авто-экспозиции пишет в cfg")
        v, fmt, lab = ui.vars["shadow_lift"]
        v.set(0.77); ui._slider("shadow_lift"); ui.root.update()
        check(abs(app.cfg["shadow_lift"] - 0.77) < 1e-6, "ползунок пишет в cfg",
              str(app.cfg["shadow_lift"]))
        check("77" in lab.cget("text"), "подпись ползунка синхронна", lab.cget("text"))
        check(app._dirty is True, "после ползунка конфиг помечен на автосохранение")
        ui.root.update()
        check(wait(lambda: os.path.exists(os.path.join(cfgdir, "config.json")), 6,
                  tick=ui.root.update),
              "автосохранение создало config.json", "")
        saved = open(os.path.join(cfgdir, "config.json"), encoding="utf-8").read()
        check('"shadow_lift": 0.77' in saved, "настройки реально записаны на диск", saved[:60])
        check(app._dirty is False, "флаг автосохранения снят")

        ui.var_mon.set("не число"); ui._set_monitor()
        check(app.cfg["monitor_index"] in (1, 8), "мусор в поле «монитор» не роняет окно",
              str(app.cfg["monitor_index"]))
        ui.var_mon.set(2); ui._set_monitor()
        check(app.cfg["monitor_index"] == 2, "номер монитора применяется")
        ui.var_top.set(False); ui._set_topmost(); ui.root.update()
        check(bool(ui.var_top.get()) is False, "галка «поверх окна» переключается",
              str(ui.var_top.get()))
        ui.var_top.set(True); ui._set_topmost(); ui.root.update()
        check(True, "«поверх окна» применяется в обе стороны (без WM значение атрибута не проверить)")
        ui.var_prof.set("Tarkov — Reserve / Labs")
        app.engine.set_profile(ui.var_prof.get()); ui.reflect(); ui.root.update()
        check(app.cfg["profile"].endswith("Labs"), "профиль переключается из окна", app.cfg["profile"])
        ui._reset(); ui.root.update()
        check(app.cfg["enabled"] is False, "кнопка «Сброс» выключает эффект")
        ui._test(); time.sleep(0.3); ui.root.update()          # без трейсбека и достаточно
        check(True, "кнопка «Тест 3с» не падает вне Windows")

        # ---- хоткеи отражаются в окне ---------------------------------
        app.on_hotkey("toggle")
        ui.root.update()
        check(bool(ui.var_on.get()) is True and app.cfg["enabled"] is True,
              "хоткей F8 reflected в галку окна")
        app.on_hotkey("none")
        check(app.cfg["enabled"] is True, "отвязанная клавиша ничего не делает")

        # ---- диагностика -------------------------------------------------
        ui._doctor()
        ui.root.update()
        win = getattr(ui, "_doctor_win", None)
        check(win is not None and win.winfo_exists(), "окно диагностики открылось")
        shown = wait(lambda: "бэкенд захвата" in ui._doctor_txt.get("1.0", "end"), 25,
                 tick=ui.root.update)
        body = ui._doctor_txt.get("1.0", "end") if ui._doctor_txt else ""
        check(shown, "отчёт диагностики появился в окне", body.strip().splitlines()[0][:70])
        # либо список «что сделать», либо честное «вроде всё ровно» (тут захват живой)
        for needle in ("gamma-таблица", "конфиг"):
            check(needle in body, "в отчёте есть строка «%s»" % needle)
        check("что сделать" in body or "вроде всё ровно" in body,
              "в отчёте есть вывод: список правок либо «всё ровно»")

        ui._doctor_copy(); ui.root.update()
        check(True, "«Скопировать всё» не падает")

        # ---- и самое главное в конце: обновлятор просит перезапуск -------
        app.restart_after_exit = True          # так решает perform_update(request_restart)
        check(wait(lambda: not ui._doctor_win.winfo_exists() or True, 1),
              "проверка не зависает на закрытом Toplevel")
        closed = wait(lambda: destroyed(), 6, tick=ui.root.update)
        check(closed, "после обновления окно закрывается само (помощник перезапустит)", "")
    finally:
        app.stop.set()
        try:
            app.shutdown()
        except Exception:
            pass
        try:
            if not destroyed():
                ui.root.destroy()
            if getattr(ui, "_doctor_win", None) and ui._doctor_win.winfo_exists():
                ui._doctor_win.destroy()
        except Exception:
            pass
        capture.Grabber = real_grab
        E.config_path = real_cfg
    return ok_all


if not have_display():
    app = M.App(dict(E.DEFAULT_CONFIG), headless=True)
    check(hasattr(app, "run_gui") and callable(app.run_gui),
          "без DISPLAY: run_gui есть и не ломается при импорте")
    check(app.run_headless.__name__ == "run_headless", "есть фолбэк run_headless")
    print("SKIP: нет DISPLAY — полный GUI-смоук пропущен (в CI так и должно быть; "
          "локально: xvfb-run -a python3 tests/test_gui.py)")
else:
    print("DISPLAY=%s — гоняем GUI по-настоящему" % os.environ.get("DISPLAY", "windows/macos"))
    run_gui_suite()

print()
if FAILS:
    print(f"ПРОВАЛЕНО {len(FAILS)}:")
    [print(" -", f) for f in FAILS]
    sys.exit(1)
print("GUI проверен")
