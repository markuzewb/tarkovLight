"""Tarkov Bright — авто-подстройка гаммы/яркости для «где вообще враг».

Запуск:
    python app/main.py                  # с окошком управления
    python app/main.py --headless       # без GUI, только хоткеи
    python app/main.py --preview a.png  # посчитать и сохранить превью коррекции

Горячие клавиши (работают поверх игры, без оверлея и без инъекций):
    F8  вкл/выкл        F7  мгновенно вернуть заводскую картинку
    F9  временный буст   F10 след. профиль (ночь / Labs / день / ручная)
"""
from __future__ import annotations

import argparse
import atexit
import copy
import os
import queue
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import correction                                      # noqa: E402
import engine as E                                     # noqa: E402
import windows as W                                    # noqa: E402


class App:
    def __init__(self, cfg: dict, headless: bool = False):
        self.cfg = cfg
        self.headless = headless
        self.q: queue.Queue = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self.ramp = W.GammaRamp()
        self.sink_ready = False
        self.probe = ({"ok": True, "reason": "не проверено"} if W.IS_WINDOWS
                      else {"ok": False, "reason": "не Windows: gamma-таблицу поставить нельзя"})
        self.engine = E.BrightnessEngine(cfg, sink=self._sink)
        self.hotkeys = W.Hotkeys(self._vk_bindings(cfg["hotkeys"]))
        self._thread: threading.Thread | None = None
        self.ui = None
        self.start_minimized = False
        self.note = ""                      # что показать в окне/консоли (композиция ниже)
        self._start_note = ""
        self._loop_note = ""
        self._miss_run = 0            # подряд идущих «экран не захватить»
        self._black_run = 0           # подряд идущих полностью чёрных кадров
        self._err_run = 0             # подряд идущих исключений в цикле
        atexit.register(self.shutdown)

    def _refresh_note(self):
        """Замечания из двух мест (старт и цикл захвата) одним текстом для GUI/консоли."""
        self.note = "   ".join(x for x in (self._start_note, self._loop_note) if x)

    # ------------------------------------------------------------------
    @staticmethod
    def _vk_bindings(hot: dict) -> dict:
        names = {"F7": W.VK_F7, "F8": W.VK_F8, "F9": W.VK_F9, "F10": W.VK_F10}
        return {names[k]: v for k, v in hot.items() if k in names}

    def _sink(self, blob):
        if not W.IS_WINDOWS:
            return
        self._write_fail = getattr(self, "_write_fail", 0)
        try:
            if blob is None:
                if not self.sink_ready:
                    return                          # и так заводская таблица — не дёргаем драйвер
                self.ramp.restore()
                self.sink_ready = False
            else:
                if not self.sink_ready:
                    self.ramp.save_original()
                    self.sink_ready = True
                if not self.ramp.apply(blob):
                    # раньше отказ был молчаливым: только красная строка в окне.
                    # теперь причина (код Windows / что проверить) видна в статусе
                    self._write_fail += 1
                    if self._write_fail in (1, 30):
                        self.probe = {"ok": False, "reason": self.ramp.error_text()}
                        self._loop_note = ("таблица не принимается (%s) — см. README, раздел "
                                           "«SetDeviceGammaRamp вернул отказ»" % self.ramp.error_text())
                        self._refresh_note()
                    if self._write_fail == 30:
                        self.cfg["enabled"] = False       # не долбить драйвер 12 раз в секунду
                        self.engine.restore_screen()
                else:
                    self._write_fail = 0
        except Exception as e:                              # не роняем поток
            self.probe = {"ok": False, "reason": f"ошибка SetDeviceGammaRamp: {e}"}

    # ------------------------------------------------------------------
    def start(self):
        if W.IS_WINDOWS:
            self.probe = W.probe_gamma_support(self.ramp)
            env = self.probe.get("env") or W.gamma_env_report()
            if env.get("remote_session"):
                # В терминальной сессии gamma-таблицы нет как функции драйвера, поэтому
                # причина одна и та же в двух местах (probe.reason + note) — оставляем
                # её коротко в probe и подробно в note, чтобы вывод не дублировался.
                self.probe = {"ok": False, "env": env,
                             "reason": "сеанс RDP: программная gamma-таблица недоступна"}
                self._start_note = ("запустите TarkovBright на том ПК, ЧЕРЕД ЭКРАНОМ которого "
                                    "вы сидите: python app\\main.py --always   (или соберите exe и "
                                    "перенесите его; либо tscon 1 /dest:console — см. README). "
                                    "Ползунки, превью и ReShade-формулы тут работают как обычно")
                self.cfg["enabled"] = False
                self._refresh_note()
            if self.cfg.get("tie_to_game") and not W.game_running():
                self._start_note = ("Тарков сейчас не запущен — эффект включится сам, как только "
                                    "игра появится (или снимите галку «только когда Тарков запущен» "
                                    "/ запустите с --always).")
                self._refresh_note()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="tb-loop")
        self._thread.start()

    def _loop(self):
        if self.cfg.get("tie_to_game", True):
            def game_check():
                return W.is_tarkov_focus() or W.game_running()
        else:
            def game_check():
                return True
        import capture
        grabber = capture.Grabber(self.cfg["capture_width"], self.cfg.get("monitor_index", 1))
        period = 1.0 / max(2.0, self.cfg["update_hz"])
        try:
            while not self.stop.is_set():
                t0 = time.perf_counter()
                try:
                    self._tick(grabber, game_check)
                except Exception as e:
                    # Одиночная ошибка (драйвер отвалился, окно захвата пропало,
                    # отказ SetDeviceGammaRamp) не должна убивать фоновый поток
                    # молча: пишем причину в статус и продолжаем.
                    self._err_run += 1
                    if self._err_run in (1, 100):
                        self._loop_note = "ошибка в рабочем цикле (%s: %s)" % (type(e).__name__, e)
                        self._refresh_note()
                    if self._err_run >= 300:
                        self._loop_note = ("цикл остановлен после %d подряд ошибок (%s) — "
                                           "перезапустите программу" % (self._err_run, e))
                        self._refresh_note()
                        break
                    time.sleep(max(0.05, period))
                    continue
                self._err_run = 0
                time.sleep(max(0.001, period - (time.perf_counter() - t0)))
        finally:
            grabber.close()

    def _tick(self, grabber, game_check) -> bool:
        """Один проход: хоткеи -> захват -> авто-решение. Возвращает True, если
        кадр удалось обработать (нужно тестам; в цикле результат не используется)."""
        for name in self.hotkeys.poll():
            self.on_hotkey(name)
        if self.cfg["tie_to_game"] and not game_check():
            self.engine.restore_screen()
            return False
        frame = grabber.grab()
        if frame is None:
            self._miss_run += 1
            if self._miss_run == 20:
                self._loop_note = ("экран не захватывается (%s) — авто-подстройка "
                                   "приостановлена, ползунки работают"
                                   % (grabber.last_error or "нет бэкенда захвата"))
                self._refresh_note()
            return False
        self._miss_run = 0
        info = self.engine.step(frame)
        st = info["stats"]
        # GDI/mss не видят Exclusive Fullscreen -> чёрный кадр. Крутить на нём гамму
        # вверх бессмысленно: предупредим и вернём заводскую таблицу.
        if st is not None and st.mean < 0.003 and st.p95 < 0.012:
            self._black_run += 1
            if self._black_run == 25:
                self._loop_note = ("кадр чёрный — скорее всего игра в Exclusive Fullscreen "
                                   "(экран захватить нельзя). В настройках Таркова выберите "
                                   "Windowed / Borderless.")
                self._refresh_note()
                self.engine.restore_screen()
            return False
        if self._loop_note.startswith(("кадр чёрный", "экран не")):
            self._loop_note = ""
            self._refresh_note()
        self._black_run = 0
        if not self.headless:
            try:
                self.q.put_nowait({k: info[k] for k in
                                   ("gamma", "target", "tint", "moved", "lift", "stats")})
            except queue.Full:
                pass
        return True

    def on_hotkey(self, name: str):
        if name == "toggle":
            self.cfg["enabled"] = not self.cfg["enabled"]
            if not self.cfg["enabled"]:
                self.engine.restore_screen()
        elif name == "restore":
            self.cfg["enabled"] = False
            self.engine.restore_screen()
        elif name == "boost":
            self.engine.toggle_boost()
        elif name == "profile":
            self.engine.next_profile()
        if self.ui is not None:
            self.ui.reflect()

    def shutdown(self):
        self.stop.set()
        try:
            if W.IS_WINDOWS:
                self.ramp.restore()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def run_gui(self):
        try:
            import tkinter as tk
            from tkinter import ttk
            self.ui = Gui(self, tk, ttk)          # тут же падает, если нет дисплея
        except Exception as e:
            print(f"[TarkovBright] GUI недоступен ({type(e).__name__}: {e}) — работаю в headless")
            return self.run_headless()
        self.start()
        # start() может сам что-то поправить в конфиге (например, выключить
        # эффект в RDP-сессии) — окно строится до него, поэтому синхронизируем
        self.ui.reflect()
        try:
            self.ui.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def run_headless(self):
        self.start()
        print("[TarkovBright] работает в фоне. F8 вкл/выкл, F7 сброс, F9 буст, F10 профиль. Ctrl+C — выход.")
        print("[TarkovBright] гамма-таблица:", "OK —" if self.probe.get("ok") else "ПРОБЛЕМА —",
              self.probe.get("reason", ""))
        if self.note:
            print("[TarkovBright] " + self.note)
        if not W.IS_WINDOWS:
            print("[TarkovBright] не Windows: таблица не ставится, hotkeys молчат — но расчёт живой.")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()


# --------------------------------------------------------------------------
class Gui:
    SLIDERS = [
        ("auto_strength", "Сила авто-яркости", 0.20, 1.50, "{:.2f}"),
        ("target_p25", "Цель для теней (p25)", 0.20, 0.45, "{:.3f}"),
        ("gamma_max", "Макс. гамма (потолок)", 1.10, 2.60, "{:.2f}"),
        ("manual_gamma", "Гамма вручную (когда авто выкл)", 0.90, 2.20, "{:.2f}"),
        ("shadow_lift", "Подъём чёрных (виньетка)", 0.00, 1.00, "{:.0%}"),
        ("saturation", "Насыщенность", 0.85, 1.45, "{:.2f}"),
        ("contrast", "Контраст", 0.90, 1.25, "{:.2f}"),
        ("knee", "Приглушать пересветы", 0.00, 1.00, "{:.0%}"),
        ("tint_strength", "Убрать зелёно-синий тилт", 0.00, 1.00, "{:.0%}"),
    ]

    def __init__(self, app: App, tk, ttk):
        self.app, self.tk, self.ttk = app, tk, ttk
        self.root = tk.Tk()
        self.root.title("Tarkov Bright — авто-гамма")
        self.root.attributes("-topmost", True)
        self.vars: dict = {}
        self._build()
        if getattr(app, "start_minimized", False):
            self.root.after(300, self.root.iconify)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._pump)

    def _build(self):
        tk, ttk = self.tk, self.ttk
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        self.var_on = tk.BooleanVar(value=self.app.cfg["enabled"])
        ttk.Checkbutton(top, text="Включено (F8)", variable=self.var_on,
                        command=lambda: self._set("enabled", self.var_on.get())).pack(side="left")
        self.var_game = tk.BooleanVar(value=self.app.cfg["tie_to_game"])
        ttk.Checkbutton(top, text="только когда Тарков запущен", variable=self.var_game,
                        command=lambda: self._set("tie_to_game", self.var_game.get())).pack(side="left", padx=10)
        self.var_auto = tk.BooleanVar(value=self.app.cfg["auto_exposure"])
        ttk.Checkbutton(top, text="Авто-экспозиция", variable=self.var_auto,
                        command=lambda: self._set("auto_exposure", self.var_auto.get())).pack(side="left")
        ttk.Label(top, text="монитор:").pack(side="left", padx=(14, 2))
        self.var_mon = tk.IntVar(value=int(self.app.cfg.get("monitor_index", 1)))
        sp = ttk.Spinbox(top, from_=1, to=5, width=3, textvariable=self.var_mon,
                         command=lambda: self._set("monitor_index", int(self.var_mon.get())))
        sp.pack(side="left")
        ttk.Button(top, text="Тест 3с", command=self._test).pack(side="right", padx=4)
        self._panel_row(top)
        ttk.Button(top, text="Сброс (F7)", command=self._reset).pack(side="right", padx=4)

        prof = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        prof.pack(fill="x")
        ttk.Label(prof, text="Профиль:").pack(side="left")
        self.var_prof = tk.StringVar(value=self.app.cfg["profile"])
        cb = ttk.Combobox(prof, textvariable=self.var_prof, values=list(E.PROFILES.keys()),
                          state="readonly", width=30)
        cb.pack(side="left", padx=6)
        def on_profile(_evt=None):
            self.app.engine.set_profile(self.var_prof.get())
            self.reflect()
        cb.bind("<<ComboboxSelected>>", on_profile)

        body = ttk.Frame(self.root, padding=(10, 4))
        body.pack(fill="x")
        for i, (key, label, lo, hi, fmt) in enumerate(self.SLIDERS):
            row = ttk.Frame(body)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=30, anchor="w").pack(side="left")
            v = tk.DoubleVar(value=float(self.app.cfg[key]))
            self.vars[key] = (v, fmt)
            ttk.Scale(row, from_=lo, to=hi, variable=v, length=260,
                      command=lambda _s, k=key: self._slider(k)).pack(side="left")
            lab = ttk.Label(row, text="", width=7, anchor="e")
            lab.pack(side="left")
            self.vars[key] = (v, fmt, lab)
            lab.configure(text=fmt.format(float(self.app.cfg[key])))

        self.status = ttk.Label(self.root, text="", padding=(10, 6), foreground="#0a7")
        self.status.pack(fill="x")
        self.warn = ttk.Label(self.root, text="", padding=(10, 0, 10, 8), foreground="#c00",
                              wraplength=560, justify="left")
        self.warn.pack(fill="x")

    def _panel_row(self, top):
        """Яркость самой панели (WMI). Работает на ноутбуках/некоторых
        мониторах, в игре может помочь там, где гаммы уже не хватает."""
        ttk = self.ttk
        box = ttk.Frame(top)
        box.pack(side="right", padx=6)
        ttk.Label(box, text="панель:").pack(side="left")
        self.panel_val = None
        self.panel_lab = ttk.Label(box, text="—", width=5)
        self.panel_lab.pack(side="left")
        for sign, txt in ((-10, "−"), (+10, "+")):
            ttk.Button(box, text=txt, width=2,
                       command=lambda s=sign: self._panel(s)).pack(side="left")

    def _panel(self, delta):
        def go():
            if self.panel_val is None:
                cur = W.set_monitor_brightness(None)
                try:
                    self.panel_val = int("".join(ch for ch in str(cur) if ch.isdigit()))
                except ValueError:
                    self.panel_val = 70
            self.panel_val = max(1, min(100, self.panel_val + delta))
            status = W.set_monitor_brightness(self.panel_val)
            self.panel_lab.configure(text=f"{self.panel_val}% {str(status)[:18]}")
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------------
    def _set(self, key, val):
        self.app.cfg[key] = val
        self.app.engine.st.last_luts = []          # заставить пересчитать немедленно

    def _slider(self, key):
        v, fmt, lab = self.vars[key]
        val = round(v.get(), 4)
        self.app.cfg[key] = val
        lab.configure(text=fmt.format(val))
        self.app.engine.st.last_luts = []

    def _test(self):
        def go():
            blob = correction.ramp_bytes(correction.build_luts(
                gamma_factor=1.75, shadow_lift=.45, saturation=1.2, knee=.4))
            self.app.ramp.save_original()
            self.app.ramp.apply(blob)
            time.sleep(3.0)
            self.app.ramp.restore()
            self.app.engine.st.last_luts = []
        threading.Thread(target=go, daemon=True).start()

    def _reset(self):
        self.app.cfg["enabled"] = False
        self.app.engine.restore_screen()
        self.reflect()

    def _on_close(self):
        self.app.shutdown()
        self.root.destroy()

    def reflect(self):
        """Синхронизировать все виджеты с cfg (после хоткея / смены профиля)."""
        cfg = self.app.cfg
        try:
            self.var_on.set(bool(cfg["enabled"]))
            self.var_game.set(bool(cfg["tie_to_game"]))
            self.var_auto.set(bool(cfg["auto_exposure"]))
            self.var_prof.set(cfg["profile"])
            for key, (v, fmt, lab) in self.vars.items():
                val = float(cfg[key])
                v.set(val)
                lab.configure(text=fmt.format(val))
        except Exception:
            pass

    def _pump(self):
        try:
            while True:
                info = self.app.q.get_nowait()
        except queue.Empty:
            info = None
        if info:
            st = info["stats"]
            t = info["tint"]
            lift = info.get("lift", 0.0)
            self.status.configure(
                text=("кадр: med %.3f  p25 %.3f  p95 %.3f  |  гамма %.2f (цель %.2f)  |  "
                      "тилт %.2f/%.2f/%.2f  |  lift %.0f%%  |  %s  |  прим. %d")
                % (st.median, st.p25, st.p95, info["gamma"], info["target"], t[0], t[1], t[2],
                   lift * 100, "движется" if info["moved"] else "стабильно",
                   self.app.engine.st.applied))
        p = self.app.probe
        msgs = [] if p.get("ok") else ["ВНИМАНИЕ: " + p.get("reason", "")]
        if self.app.note:
            msgs.append(self.app.note)
        self.warn.configure(text="\n".join(msgs))
        self.root.after(200, self._pump)

    def mainloop(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
def preview(cfg: dict, path: str, out_path: str | None = None) -> str:
    """Офлайн-прогон авто-коррекции по файлу. Требует numpy+Pillow (только эта функция)."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:
        raise SystemExit("для --preview нужны numpy и Pillow: python -m pip install numpy Pillow "
                         "(само приложение работает и без них)") from e
    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    eng = E.BrightnessEngine(copy.deepcopy(cfg), sink=None)
    for _ in range(140):                      # прогрели авто до рабочего состояния
        info = eng.step(arr)
    lut = info["luts"]
    out = correction.apply_luts(arr, lut)
    out_path = out_path or os.path.splitext(path)[0] + "_bright.png"
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    Image.fromarray(out).save(out_path)
    a, b = correction.analyze(arr), correction.analyze(out)
    print(f"{os.path.basename(path):24s} гамма {info['gamma']:.2f}  тилт "
          + "/".join(f"{x:.2f}" for x in info["tint"])
          + f"  |  p05 {a.p05:.3f}->{b.p05:.3f}  p25 {a.p25:.3f}->{b.p25:.3f}"
          + f"  med {a.median:.3f}->{b.median:.3f}  clip_hi {b.clip_hi:.3f}")
    return out_path


def selftest() -> int:
    """Мини-проверка на машине пользователя: математика + формат таблицы + захват.

    Никаких зависимостей и даже рабочего монитора не нужно — если тут всё ok,
    значит Python и сам код в порядке, а дальше только настройки Windows.
    """
    import capture as Cp
    w, h = 160, 90
    stride = ((w * 3 + 3) // 4) * 4
    buf = bytearray(stride * h)
    for y in range(h):
        o = y * stride
        for x in range(w):
            v = 8 + y * 20 // h + (150 if (x > 100 and y < 30) else 0)
            buf[o + x * 3] = v
            buf[o + x * 3 + 1] = v
            buf[o + x * 3 + 2] = v
    fr = Cp.Frame(bytes(buf), w, h, stride=stride, bpp=3, order=(2, 1, 0))

    ok = True
    def chk(cond, label, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s%s" % ("ok" if cond else "FAIL", label, ("  " + extra) if extra else ""))

    print("Python      : %s (%s)" % (sys.version.split()[0], sys.executable))
    print("numpy       : %s" % ("есть (быстрый путь)" if correction.HAVE_NUMPY
                                else "нет — работает чистый Python"))
    eng = E.BrightnessEngine(dict(E.DEFAULT_CONFIG), sink=None)
    for _ in range(80):
        info = eng.step(fr)
    st = info["stats"]
    chk(st is not None and st.p25 < 0.35, "кадр проанализирован",
        "p05 %.3f p25 %.3f med %.3f" % (st.p05, st.p25, st.median))
    chk(1.0 < info["gamma"] <= E.DEFAULT_CONFIG["gamma_max"] + 1e-6, "авто-гамма поднялась",
        "gamma %.2f" % info["gamma"])
    lut = info["luts"][0]
    chk(len(lut) == 256 and max(lut) <= 255 and min(lut) >= 0, "LUT 256 уровней в 0..255")
    chk(all(lut[i] <= lut[i + 1] for i in range(255)), "LUT монотонна (без бандинга)")
    chk(correction.build_luts()[0] == list(range(256)), "gamma=1 -> тождественная таблица")
    blob = correction.ramp_bytes(info["luts"])
    chk(len(blob) == 1536, "размер таблицы для Windows = 1536 байт", str(len(blob)))
    g = Cp.Grabber(256, 1)
    chk(g.backend in ("gdi", "mss", "pillow", "none"), "бэкенд захвата выбран", g.backend)
    shot = g.grab()
    if shot is None:
        print("  [note] экран сейчас не захватывается (%s) — на Windows без игры так и должно быть"
              % (g.last_error or "нет бэкенда"))
    else:
        chk(shot.width <= 256 and shot.height >= 16, "экран захватывается и уменшается",
            "%dx%d" % (shot.width, shot.height))
    g.close()
    print("  [%s] SetDeviceGammaRamp: %s" % ("ok" if W.IS_WINDOWS else "note",
          "Windows — таблица применяется" if W.IS_WINDOWS else "не Windows, применяется не будет"))
    print("ИТОГ:", "всё в порядке" if ok else "ЕСТЬ ПРОБЛЕМА — пришлите этот вывод")
    return 0 if ok else 1


# Реализация в windows.fix_console (оттуда же вызывают её тесты и tools/);
# здесь — алиас, чтобы `main.fix_console()` оставался точкой входа для всего проекта.
fix_console = W.fix_console


def main(argv=None) -> int:
    fix_console()   # русские сообщения не должны ронять консоль
    ap = argparse.ArgumentParser(description="Авто-гамма для Таркова (SetDeviceGammaRamp)")
    ap.add_argument("--config", help="путь к config.json (по умолчанию %%APPDATA%%/TarkovBright)")
    ap.add_argument("--profile", help="имя профиля из " + " / ".join(E.PROFILES.keys()))
    ap.add_argument("--headless", action="store_true", help="без окна, только хоткеи")
    ap.add_argument("--always", action="store_true", help="не привязываться к запуску игры")
    ap.add_argument("--gamma", type=float, help="принудительная базовая гамма (отключает авто)")
    ap.add_argument("--minimized", action="store_true", help="стартовать свёрнутым")
    ap.add_argument("--preview", nargs="+", metavar="PNG", help="прогнать авто-коррекцию по файлам")
    ap.add_argument("--restore", action="store_true",
                    help="вернуть заводскую гамму и выйти (если цвета съехали после падения)")
    ap.add_argument("--check", action="store_true", help="проверить, ставится ли гамма-таблица, и выйти")
    ap.add_argument("--selftest", action="store_true", help="проверить математику и окружение (без монитора), и выйти")
    args = ap.parse_args(argv)

    cfg = E.load_config(args.config)
    if args.profile:
        if args.profile not in E.PROFILES:
            print("нет такого профиля. есть:", ", ".join(E.PROFILES)); return 2
        cfg["profile"] = args.profile
        cfg.update(copy.deepcopy(E.PROFILES[args.profile]))
    if args.always:
        cfg["tie_to_game"] = False
    if args.gamma:
        cfg["auto_exposure"] = False
        cfg["manual_gamma"] = args.gamma

    if args.selftest:
        return selftest()

    if args.restore:
        r = W.GammaRamp()
        ok = r.force_identity()
        print("таблица сброшена" if ok else "не удалось сбросить (не Windows или отказ API)")
        return 0 if ok else 1

    if args.preview:
        for p in args.preview:
            preview(cfg, p)
        return 0

    app = App(cfg, headless=args.headless)
    app.start_minimized = args.minimized
    if args.check:
        app.start()
        time.sleep(1.2)
        app.stop.set()
        app.shutdown()
        ok = bool(app.probe.get("ok"))
        print("проверка гамма-таблицы:", ("OK — " if ok else "ПРОБЛЕМА — ")
              + app.probe.get("reason", ""))
        env = app.probe.get("env") or W.gamma_env_report()
        print("  DC: %s | режим: %d записей на канал" % (app.ramp.source, app.ramp.ramp_mode))
        print("  " + W.human_env(env))
        for h in env.get("hints", []):
            print("  →", h)
        return 0 if app.probe.get("ok") else 1
    if args.headless:
        app.run_headless()
    else:
        app.run_gui()
    if not args.config:
        try:
            E.save_config(cfg)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
