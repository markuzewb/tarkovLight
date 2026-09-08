"""python3 tests/test_gui.py — GUI-смоук. Требует дисплей; без него скипается
(в CI/песочнице дисплея нет — тогда проверяется хотя бы фолбэк в headless)."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app")); sys.path.insert(0, os.path.join(ROOT, "tools"))

import engine as E, make_samples as MS, main as M
import windows as W               # noqa: E402
W.fix_console()                   # русские print не должны падать в cp1252-консоли

if os.environ.get("DISPLAY") or sys.platform.startswith("win") or sys.platform == "darwin":
    import tkinter
    try:
        root = tkinter.Tk(); root.destroy()
    except Exception as e:
        print("SKIP: дисплей объявлен, но Tk не поднимается:", e); sys.exit(0)
    app = M.App(dict(E.DEFAULT_CONFIG), headless=False)
    import capture
    real = capture.Grabber
    class G:
        def __init__(self, *a, **k): pass
        def grab(self): return MS.scene_forest_dusk()
        def close(self): pass
    capture.Grabber = G
    sent = []
    app.engine.sink = lambda b: sent.append(b)
    ui = M.Gui(app, tkinter, __import__("tkinter.ttk", fromlist=["ttk"]))
    app.start()
    for _ in range(40):                      # прокручиваем обработчики без mainloop
        ui.root.update()
        import time; time.sleep(0.02)
    txt = ui.status.cget("text")
    ok = "гамма" in txt and "lift" in txt and len(sent) > 1
    print(("  ok   " if ok else "  FAIL ") + f"GUI собрался, статус живёт: {txt[:110]}")
    ui._slider("saturation"); ui._reset(); ui.reflect(); ui._pump()
    app.stop.set(); capture.Grabber = real; app.shutdown(); ui.root.destroy()
    sys.exit(0 if ok else 1)
else:
    app = M.App(dict(E.DEFAULT_CONFIG), headless=False)
    res = app.run_headless.__name__
    print(f"SKIP: нет DISPLAY — GUI не проверить здесь. Проверено, что run_gui() "
          f"падает в headless-фолбэк (тест в test_app.py). Доступный метод: {res}")
