"""Tarkov Bright — авто-подстройка гаммы/яркости для «где вообще враг».

Запуск:
    python app/main.py                  # с окошком управления
    python app/main.py --headless       # без GUI, только хоткеи
    python app/main.py --preview a.png  # посчитать и сохранить превью коррекции
    python app/main.py --doctor         # что у вас за Windows/сеанс/экран и что чинить
    python app/main.py --check-update   # есть ли на GitHub ревизия новее
    python app/main.py --update         # обновить себя и перезапуститься (кнопка в окне)

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
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import correction                                      # noqa: E402
import engine as E                                     # noqa: E402
import windows as W                                    # noqa: E402
import version as V                                    # noqa: E402

U = None                                             # обновление — опция: без него
UPDATER_ERROR = ""                                   # приложение живёт как раньше
try:
    import importlib
    U = importlib.import_module("updater")
except Exception as _upd_e:                          # noqa: BLE001
    UPDATER_ERROR = "%s: %s" % (type(_upd_e).__name__, _upd_e)


class App:
    def __init__(self, cfg: dict, headless: bool = False):
        self.cfg = cfg
        self.headless = headless
        self.q: queue.Queue = queue.Queue(maxsize=8)
        self.upd_q: queue.Queue = queue.Queue(maxsize=16)   # отчёты обновлятора для окна
        self.stop = threading.Event()
        self.ramp = W.GammaRamp()
        self.sink_ready = False
        self._probed = False              # пробу ставить один раз (см. start)
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
        self._write_fail = 0          # подряд идущих отказов SetDeviceGammaRamp
        self._dirty = False           # настройки менялись — стоит сохранить config.json
        self._saved_at = 0.0
        self.restart_after_exit = False
        atexit.register(self.shutdown)

    def _refresh_note(self):
        """Замечания из двух мест (старт и цикл захвата) одним текстом для GUI/консоли."""
        self.note = "   ".join(x for x in (self._start_note, self._loop_note) if x)

    _LOOP_NOTES = ("кадр чёрный", "экран не", "ошибка в рабочем цикле", "таблица не принимается")

    def _clear_loop_note(self):
        """Снять «страшное» сообщение, когда цикл снова работает нормально."""
        if self._loop_note.startswith(self._LOOP_NOTES):
            self._loop_note = ""
            self._refresh_note()

    # ------------------------------------------------------------------
    def mark_dirty(self):
        """Пользователь что-то поменял: сохраним конфиг в ближайшие пару секунд.

        Раньше config.json писался только при нормальном выходе — вылет окна или
        kill по кнопке «Х» теряли настройки, и со стороны это выглядело как
        «программа забыла, что я настраивал».
        """
        self._dirty = True

    def flush_config(self, force: bool = False) -> bool:
        """Записать config.json, если настройки менялись. force=True — писать всегда
        ( этим путём идёт выход из окна: там уже не до «сэкономим запись»)."""
        if not (self._dirty or force):
            return False
        if not force and not self.cfg.get("autosave", True):
            return False
        self._dirty = False
        try:
            E.save_config(self.cfg)
            self._saved_at = time.time()
            return True
        except Exception:                              # noqa: BLE001 — диск/права не роняют цикл
            return False

    def bind_hotkeys(self, hot: dict | None = None):
        """Перепривязать клавиши (из окна или после правки конфига руками)."""
        self.hotkeys = W.Hotkeys(self._vk_bindings(hot or self.cfg["hotkeys"]))


    # ------------------------------------------------------------------
    @staticmethod
    def _vk_bindings(hot: dict) -> dict:
        names = {"F7": W.VK_F7, "F8": W.VK_F8, "F9": W.VK_F9, "F10": W.VK_F10}
        return {names[k]: v for k, v in hot.items() if k in names}

    def _sink(self, blob):
        if not W.IS_WINDOWS:
            return
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
                    if self._write_fail:
                        self._clear_loop_note()
                    self._write_fail = 0
        except Exception as e:                              # не роняем поток
            self.probe = {"ok": False, "reason": f"ошибка SetDeviceGammaRamp: {e}"}

    # ------------------------------------------------------------------
    def note_start(self, text: str):
        """Добавить замечание о старте. Раньше каждое следующее затирало
        предыдущее, и в RDP-сессии главное сообщение («гамма тут недоступна,
        запускайте у монитора») исчезало, стоило только Таркову не оказаться
        запущенным."""
        if text and text not in self._start_note:
            self._start_note = (self._start_note + "   " + text).strip()
            self._refresh_note()

    def set_probe(self, probe: dict):
        """Задать результат пробы извне (тесты, GUI после смены монитора).

        Считается, что probe уже честный: реальная проба больше не делается
        (см. _probe_env), и окружение берётся из probe["env"], а не из GDI.
        """
        self.probe = dict(probe)
        self._probed = True

    def _probe_env(self) -> None:
        """Один раз проверить, ставится ли таблица, и отреагировать на RDP.

        Уже заданный снаружи probe (set_probe) не перетирается: на windows-latest
        раннер отвечает SM_REMOTE_SESSION=1, и иначе тест фонового цикла падает
        не по своей вине, а потому что приложение честно само себя выключило.
        """
        if self._probed or not W.IS_WINDOWS:
            return
        self._probed = True
        self.probe = W.probe_gamma_support(self.ramp)
        env = self.probe.get("env") or W.gamma_env_report()
        if env.get("remote_session"):
            # В терминальной сессии gamma-таблицы нет как функции драйвера, поэтому
            # причина одна и та же в двух местах (probe.reason + note) — оставляем
            # её коротко в probe и подробно в note, чтобы вывод не дублировался.
            self.probe = {"ok": False, "env": env,
                          "reason": "сеанс RDP: программная gamma-таблица недоступна"}
            self.note_start("запустите TarkovBright на том ПК, ЧЕРЕД ЭКРАНОМ которого "
                            "вы сидите: python app\\main.py --always   (или соберите exe и "
                            "перенесите его; либо tscon 1 /dest:console — см. README). "
                            "Ползунки, превью и ReShade-формулы тут работают как обычно")
            self.cfg["enabled"] = False

    def start(self):
        self._probe_env()
        if W.IS_WINDOWS and self.cfg.get("tie_to_game") and not W.game_running():
            self.note_start("Тарков сейчас не запущен — эффект включится сам, как только "
                            "игра появится (или снимите галку «только когда Тарков запущен» "
                            "/ запустите с --always).")
        if self.cfg.get("update_auto_check"):
            self.update_check(force=False)          # фоновый запрос, сеть не блокирует окно
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
        grabber = None
        key = ()
        period = 1.0 / 12.0
        try:
            while not self.stop.is_set():
                t0 = time.perf_counter()
                # «монитор» и частота из окна раньше применялись только после
                # перезапуска: пересоздаём захват, как только параметры поменялись
                k = self.grab_params()
                if k != key:
                    key = k
                    if grabber is not None:
                        try:
                            grabber.close()
                        except Exception:                       # noqa: BLE001
                            pass
                    grabber = capture.Grabber(k[0], k[1])
                    self._clear_loop_note()
                period = k[2]
                self.flush_config()
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
                if self._err_run:
                    self._err_run = 0
                    self._clear_loop_note()
                time.sleep(max(0.001, period - (time.perf_counter() - t0)))
        finally:
            if grabber is not None:
                grabber.close()

    def grab_params(self) -> tuple:
        """(ширина захвата, номер монитора, период тика) — устойчиво к мусору в конфиге."""
        cfg = self.cfg
        try:
            w = int(cfg.get("capture_width", 560))
        except (TypeError, ValueError):
            w = 560
        try:
            mon = int(cfg.get("monitor_index", 1))
        except (TypeError, ValueError):
            mon = 1
        try:
            hz = float(cfg.get("update_hz", 12))
        except (TypeError, ValueError):
            hz = 12.0
        return (max(64, w), max(1, mon), 1.0 / max(2.0, min(30.0, hz)))

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
        self._clear_loop_note()
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
            self.mark_dirty()
        elif name == "restore":
            self.cfg["enabled"] = False
            self.engine.restore_screen()
            self.mark_dirty()
        elif name == "boost":
            self.engine.toggle_boost()
        elif name == "profile":
            self.engine.next_profile()
            self.mark_dirty()
        elif name in ("", "none"):
            return                                # клавишу отвязали в конфиге
        if self.ui is not None:
            self.ui.reflect()

    def shutdown(self):
        self.stop.set()
        # дождаться потока: иначе grabber закроется посреди StretchBlt, а мы уже
        # вернём заводскую таблицу — на части драйверов это видно как флик при выходе
        th = self._thread
        if th is not None and th.is_alive():
            try:
                th.join(1.0)
            except RuntimeError:
                pass                              # join из самого потока — не ждём
        try:
            if W.IS_WINDOWS:
                self.ramp.restore()
        except Exception:
            pass
        self.flush_config(force=True)

    # ------------------------------------------------------------------
    # обновления (GitHub → этот каталог). Сеть никогда не блокирует UI:
    # всё в отдельном потоке, результат приходит в upd_q и в on_done.
    # ------------------------------------------------------------------
    def _update_supported(self) -> bool:
        return self._update_gate() is None

    def _update_gate(self) -> str:
        """'' = обновляться можно, иначе — причина, почему нельзя (в окно/лог)."""
        if U is None:
            return "updater недоступен: %s" % UPDATER_ERROR
        ok, why = U.supports_self_update()
        return "" if ok else why

    def _update_run(self, fn, on_done=None):
        def go():
            try:
                res = fn()
            except Exception as e:                       # noqa: BLE001 — в окно должен уйти текст
                res = {"ok": False, "state": "error",
                       "message": "сбой обновлятора: %s: %s" % (type(e).__name__, e)}
            res = dict(res or {})
            try:
                self.upd_q.put_nowait(res)
            except queue.Full:
                pass
            if on_done:
                try:
                    on_done(res)
                except Exception:                        # noqa: BLE001
                    pass
            if res.get("restart"):
                self.restart_after_exit = True
                self.stop.set()
        threading.Thread(target=go, daemon=True, name="tb-update").start()
        return True

    def update_check(self, force: bool = True, on_done=None) -> bool:
        """Спросить GitHub, есть ли ревизия новее. force=False — уважать кэш."""
        gate = self._update_gate()
        if gate:
            res = {"ok": False, "state": "not-applicable", "message": gate}
            self._upd_q_put(res)
            if on_done:
                on_done(res)
            return False
        return self._update_run(lambda: U.check(root=os.path.dirname(HERE), force=force), on_done)

    def update_now(self, on_done=None) -> bool:
        """Скачать архив ветки, разложить по месту, перезапуститься через помощника."""
        gate = self._update_gate()
        if gate:
            res = {"ok": False, "state": "not-applicable", "message": gate}
            self._upd_q_put(res)
            if on_done:
                on_done(res)
            return False
        return self._update_run(lambda: U.perform_update(root=os.path.dirname(HERE),
                                                          progress=self._update_progress),
                                on_done)

    def update_rollback(self, on_done=None) -> bool:
        if not self._update_supported():
            return False
        return self._update_run(lambda: dict(ok=True, state="rolled-back",
                                             message="откат: %s" % U.rollback(os.path.dirname(HERE))),
                               on_done)

    def _upd_q_put(self, res: dict):
        try:
            self.upd_q.put_nowait(res)
        except queue.Full:
            pass

    def _update_progress(self, msg: str):
        self._upd_q_put({"state": "progress", "message": str(msg)})

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
        self.root.title("%s   v%s" % (V.WINDOW_TITLE, V.__version__))
        self.root.attributes("-topmost", True)
        self.vars: dict = {}
        self._upd_running = False          # метод _upd_busy() занято именем быть не должно
        self._upd_note = ""
        self._build()
        if getattr(app, "start_minimized", False):
            self.root.after(300, self.root.iconify)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._pump)

    def _build(self):
        """Две строки управления + ползунки.

        Строки две не «красоты ради»: в одну они собрались на 1180 px, и на
        ноутбуке 1366 «Обновить» и «Сброс» уезжали за край экрана — то есть
        ровно те кнопки, без которых окно бессмысленно.
        """
        tk, ttk = self.tk, self.ttk
        try:
            self.root.minsize(640, 0)
        except Exception:                              # noqa: BLE001 — старый Tk без minsize
            pass
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 2))
        bar.pack(fill="x")
        left = ttk.Frame(bar)
        left.pack(side="left")
        right = ttk.Frame(bar)
        right.pack(side="right", padx=(6, 0))

        self.var_on = tk.BooleanVar(value=self.app.cfg["enabled"])
        ttk.Checkbutton(left, text="Включено (F8)", variable=self.var_on,
                        command=lambda: self._set("enabled", self.var_on.get())).pack(side="left")
        self.var_game = tk.BooleanVar(value=self.app.cfg["tie_to_game"])
        ttk.Checkbutton(left, text="только когда Тарков запущен", variable=self.var_game,
                        command=lambda: self._set("tie_to_game", self.var_game.get())).pack(side="left", padx=10)
        self.var_auto = tk.BooleanVar(value=self.app.cfg["auto_exposure"])
        ttk.Checkbutton(left, text="Авто-экспозиция", variable=self.var_auto,
                        command=lambda: self._set("auto_exposure", self.var_auto.get())).pack(side="left")
        self.var_top = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="поверх окна", variable=self.var_top,
                        command=self._set_topmost).pack(side="left", padx=(10, 0))
        ttk.Button(right, text="Тест 3с", command=self._test).pack(side="right", padx=2)
        ttk.Button(right, text="Сброс (F7)", command=self._reset).pack(side="right", padx=2)

        bar2 = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        bar2.pack(fill="x")
        left2 = ttk.Frame(bar2)
        left2.pack(side="left")

        ttk.Label(left2, text="Профиль:").pack(side="left")
        self.var_prof = tk.StringVar(value=self.app.cfg["profile"])
        cb = ttk.Combobox(left2, textvariable=self.var_prof, values=list(E.PROFILES.keys()),
                          state="readonly", width=28)
        cb.pack(side="left", padx=6)
        def on_profile(_evt=None):
            self.app.engine.set_profile(self.var_prof.get())
            self.app.mark_dirty()
            self.reflect()
        cb.bind("<<ComboboxSelected>>", on_profile)

        ttk.Label(left2, text="монитор:").pack(side="left", padx=(14, 2))
        self.var_mon = tk.IntVar(value=int(self.app.cfg.get("monitor_index", 1) or 1))
        sp = ttk.Spinbox(left2, from_=1, to=8, width=3, textvariable=self.var_mon,
                         command=self._set_monitor)
        sp.pack(side="left")
        # IntVar + Spinbox: если вписать в поле «пять», tk бросает TclError прямо
        # в колбэке. Проверяем ввод и молча игнорируем то, что не число.
        try:
            sp.configure(validate="key", validatecommand=(self.root.register(
                lambda s: s == "" or (s.isdigit() and len(s) <= 2)), "%P"))
        except Exception:                              # noqa: BLE001 — не из-за валидатора жить
            pass
        self._panel_row(left2)

        body = ttk.Frame(self.root, padding=(10, 4))
        body.pack(fill="x")
        for i, (key, label, lo, hi, fmt) in enumerate(self.SLIDERS):
            row = ttk.Frame(body)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=30, anchor="w").pack(side="left")
            v = tk.DoubleVar(value=float(self.app.cfg[key]))
            self.vars[key] = (v, fmt)
            ttk.Scale(row, from_=lo, to=hi, variable=v, length=280,
                      command=lambda _s, k=key: self._slider(k)).pack(side="left")
            lab = ttk.Label(row, text="", width=7, anchor="e")
            lab.pack(side="left")
            self.vars[key] = (v, fmt, lab)
            lab.configure(text=fmt.format(float(self.app.cfg[key])))

        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        bottom.pack(fill="x")
        self._update_row(bottom)
        ttk.Button(bottom, text="Диагностика", command=self._doctor).pack(side="right")

        self.status = ttk.Label(self.root, text="", padding=(10, 6), foreground="#0a7",
                                wraplength=760, justify="left")
        self.status.pack(fill="x")
        self.warn = ttk.Label(self.root, text="", padding=(10, 0, 10, 8), foreground="#c00",
                              wraplength=620, justify="left")
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
    # обновления: кнопки «Проверить» / «Обновить» / «Откатить»
    # ------------------------------------------------------------------
    def _update_row(self, parent):
        ttk = self.ttk
        box = ttk.Frame(parent)
        box.pack(side="left")
        self.upd_lab = ttk.Label(box, text="v%s" % V.__version__, foreground="#557")
        self.upd_lab.pack(side="left", padx=(0, 8))
        for btn in (ttk.Button(box, text="Проверить", width=9, command=self._upd_check),
                    ttk.Button(box, text="Обновить", width=10, command=self._upd_now),
                    ttk.Button(box, text="Откатить", width=9, command=self._upd_rollback)):
            btn.pack(side="left", padx=2)
        # ссылки на кнопки нужны тестам и чтобы запирать их на время работы
        self.btn_upd_check, self.btn_upd, self.btn_rollback = box.winfo_children()[1:4]
        # Для собранного .exe «Обновить» бессмысленно, зато полезна ссылка:
        # одна кнопка, которая открывает страницу релиза (там TarkovBright.exe).
        self.btn_upd_open = None
        gate = self.app._update_gate()
        if gate:
            b = ttk.Button(box, text="Скачать", width=9, command=self._upd_open)
            b.pack(side="left", padx=(6, 0))
            self.btn_upd_open = b
            self._upd_text(gate, "#a60")
            for btn in (self.btn_upd, self.btn_upd_check, self.btn_rollback):
                btn.configure(state="disabled")

    def _upd_text(self, msg: str, color: str = "#557"):
        try:
            self.upd_lab.configure(text="v%s · %s" % (V.__version__, msg), foreground=color)
        except Exception:                                   # noqa: BLE001 — окно могли закрыть
            pass

    def _upd_busy(self, busy: bool):
        self._upd_running = bool(busy)
        for b in (self.btn_upd, self.btn_upd_check, self.btn_rollback):
            try:
                b.configure(state="disabled" if busy else "normal")
            except Exception:                               # noqa: BLE001
                pass

    def _upd_open(self):
        """Открыть в браузере то, что реально можно сделать с этой копией."""
        url = getattr(self, "_upd_url", "") or (U.RELEASE_PAGE.format(repo=V.REPO) if U else "")
        if not url:
            return
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:                                   # noqa: BLE001 — нет браузера, просто покажем ссылку
            self._upd_text("откройте вручную: %s" % url, "#a60")
            self._upd_note = "ссылка: " + url

    def _upd_check(self):
        if self._upd_running or U is None or self.app._update_gate():
            return
        self._upd_busy(True)
        self._upd_text("спрашиваю GitHub…")
        self.app.update_check(force=True)

    def _upd_now(self):
        if self._upd_running or U is None or self.app._update_gate():
            return
        try:
            from tkinter import messagebox
            go = messagebox.askyesno(
                "Обновить TarkovBright?",
                "Скачаю архив ветки %s из %s, заменю файлы программы "
                "(ваши настройки и скриншоты не трону; оригиналы уйдут в _update\\backup-*, "
                "откат — кнопкой «Откатить»).\n\nПрограмма перезапустится сама. "
                "Неудобно прямо сейчас — нажмите «Нет»." % (V.BRANCH, V.REPO))
        except Exception:                                   # noqa: BLE001 — нет диалогов, всё равно обновляем
            go = True
        if not go:
            self._upd_text("обновление отменено")
            return
        self._upd_busy(True)
        self._upd_text("скачиваю…")
        self.app.update_now()

    def _upd_rollback(self):
        if self._upd_running or U is None or self.app._update_gate():
            return
        self._upd_busy(True)
        self._upd_text("откатываю файлы…")
        self.app.update_rollback()

    def _upd_handle(self, res: dict):
        """Очередь обновлятора -> подписи в окне. Вызывается только из mainloop."""
        state = res.get("state", "")
        msg = str(res.get("message", "") or "")
        self._upd_url = str(res.get("exe_url") or res.get("url") or getattr(self, "_upd_url", ""))
        if res.get("exe_url") and self.btn_upd_open is not None:
            self.btn_upd_open.configure(text="Скачать exe")
        if state == "doctor":
            self._doctor_show(str(res.get("text", "")))
            return
        if state == "progress":
            self._upd_text(msg[:110], "#36c")
            return
        self._upd_busy(False)
        if state == "not-applicable":
            self._upd_text(msg[:110], "#a60")
            self._upd_note = msg                       # ссылку — целиком, без обрезки
            return
        if state in ("no-releases", "not-applicable"):
            self._upd_text(msg[:110] or "релизов ещё нет", "#a60")
            return
        if state == "up-to-date":
            self._upd_text("обновлений нет", "#0a7")
        elif state in ("update-available", "unknown", "applied", "rolled-back"):
            self._upd_text(msg[:110] or "готово", "#0a7")
        elif state == "offline":
            self._upd_text(msg[:110] or "нет связи", "#a60")
        else:
            self._upd_text(msg[:110] or "ошибка", "#c00")
        if res.get("exe_url") and res.get("state") == "update-available":
            self._upd_note = "скачать новый файл: " + res["exe_url"]
        if res.get("applied"):
            self._upd_note = ("обновлено файлов: %d; оригиналы — в _update\\backup-* "
                              "(«Откатить»)." % len(res["applied"]))
            if not res.get("restart"):
                self._upd_note += " Перезапустите программу, чтобы новый код заработал."
        if res.get("errors"):
            self._upd_note = "не заменилось: " + "; ".join(res["errors"][:2])
        elif res.get("backup_dir"):
            self._upd_note = ""

    def _doctor(self):
        """Окошко с диагностикой. Считается в потоке: один tasklist может думать до 2 с,
        вешать на это UI нельзя."""
        tk = self.tk
        if not hasattr(self, "_doctor_win") or not self._doctor_win:
            try:
                win = tk.Toplevel(self.root)
                win.title("Диагностика TarkovBright — v%s" % V.__version__)
                win.geometry("760x460")
                txt = tk.Text(win, wrap="word", font=("Consolas", 10), padx=10, pady=8)
                txt.pack(fill="both", expand=True)
                bar = tk.Frame(win)
                bar.pack(fill="x")
                tk.Button(bar, text="Пересчитать", command=self._doctor_refresh).pack(side="left", padx=8, pady=4)
                tk.Button(bar, text="Скопировать всё", command=self._doctor_copy).pack(side="left", pady=4)
                tk.Label(bar, text="пришлите этот текст, если что-то не работает",
                         foreground="#666").pack(side="right", padx=8)
                self._doctor_win, self._doctor_txt = win, txt
            except Exception:                              # noqa: BLE001 — нет Tk, печатаем в консоль
                print(doctor(app=self.app, network=True))
                return
        self._doctor_refresh()

    def _doctor_refresh(self):
        txt = getattr(self, "_doctor_txt", None)
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("1.0", "считаю (пара секунд: смотрю сеанс, экран, процесс игры, GitHub)…\n")
        txt.configure(state="disabled")

        def go():
            try:
                body = doctor(app=self.app, network=True)
            except Exception as e:                           # noqa: BLE001
                body = "диагностика упала: %s: %s\nпришлите этот текст" % (type(e).__name__, e)
            # НЕ root.after из потока: Tkinter не тредобезопасен, а из рабочего
            # потока это то работает, то кидает «main thread is not in main loop».
            # Очередь статуса уже есть в App — пользуемся ею же.
            try:
                self.app.upd_q.put_nowait({"state": "doctor", "text": body})
            except queue.Full:
                pass

        threading.Thread(target=go, daemon=True, name="tb-doctor").start()

    def _doctor_show(self, body: str):
        txt = getattr(self, "_doctor_txt", None)
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("1.0", "TarkovBright v%s — диагностика\n%s\n" % (V.__version__, body))
        txt.configure(state="disabled")

    def _doctor_copy(self):
        txt = getattr(self, "_doctor_txt", None)
        if txt is None:
            return
        try:
            txt.tag_add("sel", "1.0", "end")
            txt.event_generate("<<Copy>>")
            txt.tag_remove("sel", "1.0", "end")
        except Exception:                                    # noqa: BLE001 — буфер обмена не критичен
            pass

    def _set(self, key, val):
        self.app.cfg[key] = val
        self.app.mark_dirty()
        self.app.engine.st.last_luts = []          # заставить пересчитать немедленно

    def _set_monitor(self):
        try:
            v = int(self.var_mon.get())
        except Exception:                          # noqa: BLE001 — IntVar.get() бросает TclError
            return                                 # на любом мусоре в поле; «пять» != падение
        self._set("monitor_index", max(1, min(8, v)))

    def _set_topmost(self):
        try:
            self.root.attributes("-topmost", bool(self.var_top.get()))
        except Exception:                          # noqa: BLE001 — Tk без -topmost бывает
            pass

    def _slider(self, key):
        v, fmt, lab = self.vars[key]
        try:
            val = round(float(v.get()), 4)
        except (TypeError, ValueError):
            return
        self.app.cfg[key] = val
        lab.configure(text=fmt.format(val))
        self.app.mark_dirty()
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
        self.app.mark_dirty()
        self.reflect()

    def _on_close(self):
        self.app.shutdown()
        self.root.destroy()

    def _after_restart(self):
        """Обновлятор попросил перезапуск: чиним экран, сохраняемся и выходим.

        Сам подъём нового процесса делает _update_helper — он дожидается нашего
        выхода (иначе Windows не отдаёт занятые файлы) и, если что, докладывает
        то, что не заменилось с первого раза.
        """
        try:
            self.app.flush_config(force=True)
            self.app.shutdown()
        finally:
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
        # Важно: `info` зануляется ДО draining-цикла. Раньше в except стояло
        # `info = None`, и последний изъятый кадр терялся — статус окна не
        # обновлялся никогда (на CI это не ловилось: без дисплея тест скипался).
        info = None
        try:
            while True:
                info = self.app.q.get_nowait()
        except queue.Empty:
            pass
        try:
            while True:
                self._upd_handle(self.app.upd_q.get_nowait())
        except queue.Empty:
            pass
        if self.app.restart_after_exit:
            self.root.after(120, self._after_restart)
            self.app.restart_after_exit = False
            return
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
        if self._upd_note:
            msgs.append(self._upd_note)
        self.warn.configure(text="\n".join(msgs))
        self.root.after(200, self._pump)

    def mainloop(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
# «Диагностика»: одним взглядом видно, что у вас и что чинить
# --------------------------------------------------------------------------
def doctor(app=None, cfg: dict | None = None, network: bool = True) -> str:
    """Отчёт по окружению. Меняет ровно ничего (гамму трогает только эмпирическая
    проба, та же, что в `--check`; она возвращает таблицу обратно)."""
    cfg = cfg if cfg is not None else (app.cfg if app is not None else dict(E.DEFAULT_CONFIG))
    rows = []
    tips = []

    def add(label, ok, text=""):
        rows.append("  [%-4s] %-22s %s" % ("ok" if ok else ("FAIL" if ok is False else "note"),
                                            label, text))
        return ok

    frozen = bool(getattr(sys, "frozen", False))
    add("версия/сборка", None, "v%s, %s, python %s%s" % (
        V.__version__, "exe" if frozen else "обычный запуск",
        sys.version.split()[0], ""))
    if not frozen and sys.version_info[:2] < (3, 9):
        add("python", False, "нужен 3.9+: winget install -e --id Python.Python.3.12")
        tips.append("обновите Python до 3.9+ (сейчас %s)" % sys.version.split()[0])

    rep = W.session_report() if W.IS_WINDOWS else {"note": "не Windows"}
    env = W.gamma_env_report() if W.IS_WINDOWS else {}
    add("ОС/сеанс", None, ("%s | %s" % (rep.get("os", ""), W.human_env(env))).strip(" |"))
    if env.get("remote_session"):
        tips.append("вы в RDP: gamma-таблицы в терминальной сессии нет — запускайте программу "
                    "на том ПК, перед которым сидите (или tscon 1 /dest:console, или Moonlight)")

    probe = app.probe if app is not None else (
        W.probe_gamma_support(W.GammaRamp()) if W.IS_WINDOWS
        else {"ok": False, "reason": "не Windows — таблица не ставится"})
    add("gamma-таблица", bool(probe.get("ok")), str(probe.get("reason", ""))[:150])
    if not probe.get("ok"):
        for h in (probe.get("env") or env).get("hints", [])[:2]:
            tips.append(str(h))

    # «кто ещё держит таблицу»: ночной свет/f.lux/панель драйвера пишут в ту же LUT
    if W.IS_WINDOWS and app is not None and not app.ramp.active:
        try:
            cur = app.ramp._read()
            if cur is not None and cur != correction.identity_ramp():
                add("таблица уже выкручена", False,
                    "текущая LUT не 1:1, хотя эффект выключен — её держит «Ночной свет»/f.lux "
                    "или панель драйвера; верните: python app\\main.py --restore")
                tips.append("выключите «Ночной свет» (параметры → Дисплей → Ночной свет) — "
                            "он владеет той же таблицей и перекрывает эффект")
            else:
                add("таблица", None, "1:1 (заводская), никто не мешает")
        except Exception as e:                              # noqa: BLE001
            add("таблица", None, "не прочитать: %s" % e)

    import capture as Cp
    if app is not None:
        w, mon, _period = app.grab_params()
    else:
        w, mon = (cfg.get("capture_width", 560), cfg.get("monitor_index", 1))
    try:
        w, mon = int(w), int(mon)
    except (TypeError, ValueError):
        w, mon = 560, 1
    g = Cp.Grabber(w, mon)
    try:
        # getattr, а не прямое обращение: «захват» может быть подменён чем угодно
        # (тесты, будущий новый бэкенд) — диагностика не имеет права на это падать
        be = str(getattr(g, "backend", "?"))
        err = str(getattr(g, "last_error", "") or "")
        add("бэкенд захвата", be not in ("none", "?"), "%s%s" % (
            be, "" if be not in ("none", "?") else " (%s)" % (err or "нет источников")))
        fr = g.grab()
        if fr is None:
            add("кадр экрана", False, "не захватывается: %s" % (err or "нет бэкенда"))
            tips.append("захвата нет: в RDP/на сервере без рабочего стола это нормально; "
                        "на рабочей машине проверьте, что не «Безопасный рабочий стол»")
        else:
            st = correction.analyze(fr, float(cfg.get("center_frac", 0.72)))
            black = st.mean < 0.003 and st.p95 < 0.012
            add("кадр экрана", not black, "%dx%d  med %.3f p25 %.3f%s" % (
                fr.width, fr.height, st.median, st.p25,
                "" if not black else "  — ЧЁРНЫЙ (Exclusive Fullscreen?)"))
            if black:
                tips.append("кадр чёрный: в настройках Таркова выберите Windowed / Borderless")
    except Exception as e:                                  # noqa: BLE001
        add("кадр экрана", False, "проверка не удалась: %s: %s" % (type(e).__name__, e))
    finally:
        try:
            g.close()
        except Exception:                                   # noqa: BLE001
            pass

    if cfg.get("tie_to_game"):
        run = W.game_running() if W.IS_WINDOWS else False
        add("Тарков запущен", None, "да" if run else "нет (эффект спит, пока игра не появится)")
        if not run and W.IS_WINDOWS:
            tips.append("эффект включится, когда игра появится; запускать везде — флаг --always")
    else:
        add("привязка к игре", None, "выключена (работает всегда)")

    add("эффект", bool(cfg.get("enabled")), "включён" if cfg.get("enabled")
        else "выключен — F8 или галка «Включено»")
    if not cfg.get("enabled") and probe.get("ok"):
        tips.append("проба прошла, но эффект выключен: включите галку «Включено» (F8)")
    add("профиль", None, str(cfg.get("profile", "")))

    path = E.config_path()
    add("конфиг", os.path.isfile(path), path + ("" if os.path.isfile(path) else " (ещё не создан)"))

    if network and U is not None:
        try:
            stt = U.check(root=os.path.dirname(HERE), force=False)
            add("обновления", stt.get("ok", True), "%s · %s" % (stt.get("state"), str(stt.get("message"))[:90]))
            if stt.get("state") == "update-available":
                tips.append("на GitHub есть ревизия новее — кнопка «Обновить» (или main.py --update)")
        except Exception as e:                              # noqa: BLE001
            add("обновления", None, "не проверил: %s" % e)
    elif network:
        add("обновления", None, "обновлятор не поднят: %s" % UPDATER_ERROR)

    out = ["\n".join(rows)]
    if tips:
        out.append("\n  что сделать:")
        out += ["   %d) %s" % (i + 1, t) for i, t in enumerate(dict.fromkeys(tips))]
    else:
        out.append("\n  вроде всё ровно: гамма ставится, кадр есть, конфиг на месте.")
    return "\n".join(out)

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




# --------------------------------------------------------------------------
# «один файл»: самоспасение собранного .exe
# --------------------------------------------------------------------------
FROZEN = bool(getattr(sys, "frozen", False))


def _boot_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, V.APP_NAME)


def _boot_log(msg: str) -> None:
    """Писать в %APPDATA%\\TarkovBright\\error.log всегда: и когда stdout есть,
    и когда его нет (--noconsole у PyInstaller: print в никуда)."""
    try:
        os.makedirs(_boot_dir(), exist_ok=True)
        with open(os.path.join(_boot_dir(), "error.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass
    try:
        if sys.stdout is not None:
            print(msg)
    except Exception:                                       # noqa: BLE001
        pass


def _boot_error(text: str) -> None:
    """Показать причину человеку: Tk -> MessageBoxW -> только лог. В --noconsole
    без этого double click выглядит как «файл не запускается вообще»."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk(); r.withdraw()
        messagebox.showerror("TarkovBright — не запустилось", text)
        r.destroy()
        return
    except Exception:                                       # noqa: BLE001
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "TarkovBright", 0x10)
        except Exception:                                   # noqa: BLE001
            pass


def main(argv=None) -> int:
    if not FROZEN:
        return _main_body(argv)          # .py / .pyw: логирует и показывает сам вход
    try:
        return _main_body(argv)
    except SystemExit:
        raise                            # argparse и --selftest: штатный выход
    except KeyboardInterrupt:
        return 130
    except BaseException:                # noqa: BLE001 — exe не должен умирать молча
        text = traceback.format_exc()
        _boot_log("исключение в собранном exe:\n" + text)
        _boot_error("TarkovBright не смог работать:\n\n"
                    + (text.strip().splitlines() or ["?"])[-1]
                    + "\n\nПолный текст — в %s.\nЕсли не хватает прав на запись — "
                      "запустите «От администратора» или перенесите файл в свой профиль."
                    % os.path.join(_boot_dir(), "error.log"))
        return 1


def _main_body(argv=None) -> int:
    fix_console()   # русские сообщения не должны ронять консоль (cp1252)
    ap = argparse.ArgumentParser(
        description="Авто-гамма для Таркова (SetDeviceGammaRamp) · v" + V.__version__)
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
    ap.add_argument("--doctor", action="store_true", help="отчёт «что за сеанс/экран/конфиг и что чинить», и выйти")
    ap.add_argument("--version", action="store_true", help="версия и путь, по которому живёт программа")
    ap.add_argument("--check-update", action="store_true",
                    help="спросить GitHub, есть ли ревизия новее (то же, что кнопка «Проверить»)")
    ap.add_argument("--update", action="store_true",
                    help="скачать архив ветки, заменить файлы и перезапуститься (кнопка «Обновить»)")
    ap.add_argument("--rollback", action="store_true", help="вернуть файлы из последней копии перед обновлением")
    ap.add_argument("--no-restart", action="store_true", help="с --update: не перезапускаться самому")
    ap.add_argument("--no-net", action="store_true", help="не лезть в сеть (ни обновлений, ни проверки)")
    ap.add_argument("--force-multi", action="store_true",
                    help="разрешить второй экземпляр (не рекомендуется: они дерутся за gamma-таблицу)")
    args = ap.parse_args(argv)

    if args.version:
        if FROZEN:
            # для .exe путь «где код» — временная распаковка, он ничего не объясняет;
            # важнее то, что это один файл и чем он обновляется
            print("%s v%s\nсобран в один файл: %s\nконфиг: %s\nобновления: заменить файл — %s%s"
                  % (V.APP_NAME, V.__version__, sys.executable, E.config_path(),
                     U.EXE_URL.format(repo=V.REPO) if U else "(updater недоступен)",
                     "" if U is not None else "\nобновлятор недоступен: " + UPDATER_ERROR))
        else:
            print("%s v%s\nкод: %s\nконфиг: %s\nобновления: %s (%s)%s" % (
                V.APP_NAME, V.__version__, os.path.dirname(HERE), E.config_path(),
                V.REPO, V.BRANCH,
                "" if U is not None else "\nобновлятор недоступен: " + UPDATER_ERROR))
        return 0

    warns: list = []
    cfg = E.load_config(args.config, warn=warns.append)
    if args.no_net:
        cfg["update_auto_check"] = False
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
    if not cfg.get("autosave", True):
        cfg["update_auto_check"] = False

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

    if args.doctor:
        print(doctor(cfg=cfg, network=not args.no_net))
        return 0

    # --- путь обновлятора: без окна и без захвата, чтобы работало и на битом окружении
    if args.check_update or args.update or args.rollback:
        if U is None:
            print("авто-обновление недоступно: %s" % UPDATER_ERROR)
            return 2
        root = os.path.dirname(HERE)
        ok_ss, why_ss = U.supports_self_update()
        if args.update and not ok_ss:
            print("в этом запуске само-обновление бессмысленно: %s" % why_ss)
            return 2
        if args.no_net and (args.check_update or args.update):
            print("сеть отключена (--no-net): проверить GitHub и скачать архив не могу. "
                  "Обновитесь без этого флага или скачайте репозиторий руками.")
            return 2
        if args.rollback:
            rep = U.rollback(root)
            if rep.get("restored"):
                print("вернул файлы из %s: %s" % (rep["from"], ", ".join(rep["restored"][:6])))
                print("перезапустите программу.")
                return 0
            print("откат не получился: %s" % (rep.get("reason") or "; ".join(rep.get("errors", []))))
            return 1
        if args.check_update:
            st = U.check(root=root, force=True)
            print(U.human(st))
            return 0 if st.get("ok") else 1
        rep = U.perform_update(root=root, restart=not args.no_restart,
                              progress=lambda m: print("  · %s" % m))
        if rep.get("ok"):
            print(rep.get("message", "готово"))
            if rep.get("restart"):
                print("закрываюсь; помощник подменит остатки и запустит заново.")
            return 0
        print("НЕ ОБНОВЛЕНО: " + str(rep.get("message", "")))
        return 1

    # --- один экземпляр: два процесса за одну gamma-таблицу — это испорченные цвета
    if not args.force_multi and not args.check:
        ok, detail = W.acquire_instance_lock()
        if not ok:
            msg = ("TarkovBright уже запущен.\n\n%s\n\nДва экземпляра дерутся за gamma-таблицу: "
                   "второй сохранит «оригинал», уже выкрученный первым, и после выхода обоих "
                   "цвета останутся чужими.\n\nЗакройте старое окно (или запустите новый "
                   "с флагом --force-multi, если вам правда надо так)." % detail)
            print("[TarkovBright] " + msg.replace("\n\n", " ").replace("\n", " "))
            if W.IS_WINDOWS:
                try:                                   # в pythonw консоли нет — ругань видна только так
                    __import__("ctypes").windll.user32.MessageBoxW(0, msg, "TarkovBright уже запущен", 0x30)
                except Exception:
                    pass
            return 3

    app = App(cfg, headless=args.headless)
    app.start_minimized = args.minimized
    for w in warns:
        app.note_start("конфиг: " + w)
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
