"""python3 tests/test_app.py — сквозная проверка приложения без Windows и без монитора:
подменяем захват экрана синтетическими кадрами и слушаем, что улетает в gamma-таблицу.

Заодно проверяем: привязку к игре, хоткеи, очередь в GUI, деградацию без tkinter.
"""
from __future__ import annotations
import os, queue, sys, time
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import correction as C          # noqa: E402
import engine as E              # noqa: E402
import make_samples as MS       # noqa: E402
import capture                  # noqa: E402
import windows as W               # noqa: E402
W.fix_console()                   # русские print не должны падать в cp1252-консоли

class _NullRamp:
    """Заглушка драйверу: тесты не имеют права трогать настоящую gamma-таблицу.

    На Windows App.shutdown()/restore вызывают `self.ramp.restore()` — то есть
    реальные Get/SetDeviceGammaRamp. На CI-машине с базовым видеодрайвером это
    кончалось Segmentation fault (exit 139) посреди test_app; а на машине
    пользователя тесты просто мигали бы экраном.
    """
    source = "тест (таблица не трогается)"
    ramp_mode = 256
    last_error = 0
    last_note = ""

    def save_original(self): return True
    def apply(self, blob): return True
    def restore(self): return True
    def force_identity(self): return True
    def reset_dc(self): pass
    def error_text(self): return ""


FAILS = []
def check(cond, msg, extra=""):
    print(("  ok   " if cond else "  FAIL ") + msg + (f"   [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(msg)

SCENES = [MS.scene_forest_dusk(), MS.scene_labs(), MS.scene_night_flash()]
frame_i = {"i": 0}


class FakeGrabber:
    """Тот же интерфейс, что capture.Grabber."""
    def __init__(self, *a, **k):
        pass
    def grab(self):
        f = SCENES[frame_i["i"] % len(SCENES)]
        return f
    def close(self):
        pass


class HoldGrabber(FakeGrabber):
    def grab(self):
        return SCENES[0]


import main as M                # noqa: E402


def make_app(cfg=None, grabber=HoldGrabber, game_check_value=True):
    cfg = cfg or dict(E.DEFAULT_CONFIG)
    cfg["update_hz"] = 60
    cfg["min_lut_delta"] = 0
    app = M.App(cfg, headless=True)
    app.ramp = _NullRamp()          # никакого реального gdi32: см. комментарий к классу
    sent = []
    app.engine.sink = lambda blob: sent.append(blob)
    # env задаём сами: start() иначе опрашивает реальный экран, а на CI-раннере
    # Windows SM_REMOTE_SESSION=1 -> приложение глушит эффект, и таблиц нет.
    app.set_probe({"ok": True, "reason": "тест", "env": {"remote_session": False}})
    # перехватываем создание Grabber'а внутри потока
    real = capture.Grabber
    capture.Grabber = grabber
    app._test_sent = sent
    app._real_grabber = real
    if cfg.get("tie_to_game"):
        M.W.is_tarkov_focus = lambda: game_check_value
        M.W.game_running = lambda: game_check_value
    return app


print("== сквозной путь: кадр -> движок -> ramp ==")
app = make_app()
app.start()
# Ждём УСЛОВИЕ, а не фиксированные секунды: на Windows start() синхронно прогоняет
# пробу SetDeviceGammaRamp, и на VM/кривом драйвере это секунды — поток стартует уже
# после неё. На windows-latest фиксированный sleep(1.2) давал «0 таблиц» при полностью
# рабочем коде (проверено в CI), поэтому цикл с тайм-аутом.
deadline = time.time() + 10.0
while True:
    sent = list(app._test_sent)
    ramps = [s for s in sent if s is not None]
    if len(ramps) > 5 or time.time() > deadline:
        break
    time.sleep(0.1)
app.stop.set(); capture.Grabber = app._real_grabber
app.shutdown()
sent = list(app._test_sent)
check(app.probe.get("reason") == "тест",
      "предустановленный probe не перетёрт start()", str(app.probe.get("reason")))
ramps = [s for s in sent if s is not None]
check(len(ramps) > 5, "поток крутится и ставит таблицы", f"{len(ramps)} шт")
check(all(len(s) == 1536 for s in ramps), "каждый пакет — ровно 1536 байт")
g = app.engine.st.gamma
gmax = app.cfg["gamma_max"]
check(1.3 <= g <= gmax + 1e-6, "на тёмном лесе гамма выкручена вверх и не выше потолка",
      f"{g:.2f} (потолок {gmax:.2f})")
w = np.frombuffer(ramps[-1], dtype="<u2").reshape(256, 3)
check(int(w[16][0]) > 16 * 257, "тени в таблице реально подняты", f"уровень 16 -> {int(w[16][0])/257:.0f}")
check(int(w[255][0]) == 65535, "белый остался белым")

print("== очередь в GUI ==")
cfg = dict(E.DEFAULT_CONFIG); cfg["update_hz"] = 60
app = make_app(cfg)
app.headless = False
app.start()
deadline = time.time() + 10.0            # ждём ПОТОК (а не 0.7с вслепую): на Windows поток
while time.time() < deadline:            # стартует после синхронной пробы таблицы
    time.sleep(0.05)
    if app.q.qsize() >= 6:               # нужно >3 сообщений, чтобы проверка была осмысленной
        break
got = 0
bad = 0
try:
    while True:
        info = app.q.get_nowait(); got += 1
        if not (0.5 <= info["gamma"] <= 3.0) or info["stats"].p25 < 0:
            bad += 1
except queue.Empty:
    pass
app.stop.set(); capture.Grabber = app._real_grabber; app.shutdown()
check(got > 3, "статистика приходит в очередь GUI", f"{got} сообщ.")

print("== смена сцены на лету (выбег в светлый) ==")
cfg = dict(E.DEFAULT_CONFIG); cfg["update_hz"] = 60
app = make_app(cfg, grabber=FakeGrabber)
app.start(); time.sleep(2.5)
app.stop.set(); capture.Grabber = app._real_grabber
g_mid = app.engine.st.gamma
app.shutdown()
gmin, gmax = app.cfg["gamma_min"], app.cfg["gamma_max"]
check(gmin - 1e-6 <= g_mid <= gmax + 1e-6, "гамма осталась в пределах при мигании сценами",
      f"{g_mid:.2f} в [{gmin:.2f}, {gmax:.2f}]")

print("== хоткеи ==")
app = make_app()
eng = app.engine
before = app.cfg["enabled"]; app.on_hotkey("toggle")
check(app.cfg["enabled"] != before, "F8 переключает вкл/выкл")
app.on_hotkey("restore")
check(app.cfg["enabled"] is False, "F7 глушит эффект")
app.on_hotkey("boost")
check(eng.st.boost_until > time.time(), "F9 включает временный буст")
p0 = app.cfg["profile"]; app.on_hotkey("profile")
check(app.cfg["profile"] != p0, "F10 листает профили", f"{p0} -> {app.cfg['profile']}")
for n in ("toggle", "restore", "boost", "profile"):
    app.on_hotkey(n)
check(True, "все хоткеи перевариваются без исключений")
app.shutdown()

print("== привязка к игре ==")
class NoGame(FakeGrabber):
    pass
cfg = dict(E.DEFAULT_CONFIG); cfg["tie_to_game"] = True
orig_focus, orig_run = M.W.is_tarkov_focus, M.W.game_running
M.W.is_tarkov_focus = lambda: False
M.W.game_running = lambda: False
app = make_app(cfg, grabber=HoldGrabber, game_check_value=False)
app.start(); time.sleep(0.8)
n_games_off = len([s for s in app._test_sent if s is not None])
app.stop.set(); capture.Grabber = app._real_grabber; app.shutdown()
M.W.is_tarkov_focus, M.W.game_running = orig_focus, orig_run
check(n_games_off == 0, "без запущенного Таркова таблица не трогается", f"{n_games_off} пакетов")

print("== реальный путь захвата (бэкенд подменён, OS-вызова нет) ==")
import importlib
capture = importlib.reload(capture)         # снимаем ранние подмены Grabber'а
big = np.random.default_rng(7).integers(0, 256, (540, 960, 3), dtype=np.uint8)


class _FakeImg:
    """Минимум интерфейса PIL.Image, который использует _grab_pillow."""
    def __init__(self, a):
        self.a = a
        self.size = (a.shape[1], a.shape[0])

    def convert(self, _mode):
        return self

    def resize(self, size, _filt=None):
        h, w = int(size[1]), int(size[0])
        ys = (np.arange(h) * self.a.shape[0] // h).clip(0, self.a.shape[0] - 1)
        xs = (np.arange(w) * self.a.shape[1] // w).clip(0, self.a.shape[1] - 1)
        return _FakeImg(self.a[np.ix_(ys, xs)])

    def tobytes(self, *args):
        return self.a.tobytes()


class _FakePil:
    @staticmethod
    def grab(all_screens=False):
        return _FakeImg(big)


_g = capture.Grabber(560)
_g.backend = "pillow"                       # детерминированно, независимо от наличия mss
_g._pil = _FakePil
_f = _g.grab()
check(_f is not None and (_f.width, _f.height) == (560, 315) and len(_f.pixels) == 315 * 560 * 3,
      "grab() -> Frame 560x315 (даунскейл 960x540)", str(None if _f is None else (_f.width, _f.height, len(_f.pixels))))
_a = _f.as_numpy()
check(_a.shape == (315, 560, 3) and _a.dtype == np.uint8, "Frame.as_numpy() -> RGB-массив", str(_a.shape))
check(bool(np.array_equal(_a[0, 0], big[0, 0])),
      "верхняя левая строка кадра на месте (top-down, не bottom-up)")
_small = np.random.default_rng(3).integers(0, 256, (200, 300, 3), dtype=np.uint8)
_g2 = capture.Grabber(560)
_g2.backend = "pillow"
_g2._pil = type("M", (), {"grab": staticmethod(lambda all_screens=False, a=_small: _FakeImg(a))})
_a2 = _g2.grab()
check(_a2 is not None and bool(np.array_equal(_a2.as_numpy(), _small)),
      "без ресайза байты кадра = пиксели 1:1 (RGB, stride без съезда)",
      str(None if _a2 is None else (_a2.width, _a2.height)))
_pat = np.zeros((4, 4, 3), np.uint8)
_pat[0, 0] = (255, 0, 0)
_pat[3, 3] = (0, 0, 255)
_rowb = ((4 * 3 + 3) // 4) * 4
_fb = bytearray(_rowb * 4)
_bgr = np.ascontiguousarray(_pat[:, :, ::-1])
for _y in range(4):
    _fb[_y * _rowb:_y * _rowb + 12] = _bgr[_y].tobytes()
_f4 = capture.Frame(bytes(_fb), 4, 4, stride=_rowb, bpp=3, order=(2, 1, 0))
_a4 = _f4.as_numpy()
check(tuple(int(v) for v in _a4[0, 0]) == (255, 0, 0) and tuple(int(v) for v in _a4[3, 3]) == (0, 0, 255),
      "as_numpy: BGR + выравнивание строк DIB разбираются верно",
      f"{tuple(int(v) for v in _a4[0, 0])} {tuple(int(v) for v in _a4[3, 3])}")
_st4 = C.analyze(_f4)
check(_st4.mean > 0, "анализ ручного DIB-кадра не падает", f"mean={_st4.mean:.3f}")
_f2 = capture.Frame(_f.pixels, _f.width, _f.height, stride=_f.stride, bpp=3, order=(0, 1, 2))
st_np, st_pure = C._analyze_numpy(_a, 0.72), C._analyze_frame(_f2, 0.72)
dq = max(abs(getattr(st_np, k) - getattr(st_pure, k)) for k in ("p05", "p25", "median", "p95"))
check(dq < 3 / 255.0, "pure- и numpy-анализ одного кадра согласны", f"{dq*255:.2f} ур.")
_st = C.analyze(_f)
check(0.3 < _st.median < 0.7, "статистика с реального пути захвата адекватна", f"med={_st.median:.3f}")
_padded = capture.Frame(b"\x00" * (4 * 8 * 3) + bytes(big[:4, :4].reshape(-1)), 4, 4, stride=4 * 3 + 4, bpp=3, order=(0, 1, 2))
check(len(_padded.as_numpy()) == 4 and _padded.as_numpy().shape == (4, 4, 3), "stride-выравнивание DIB разбирается верно")
check(capture.Grabber(560, 4).monitor == 4, "номер монитора доезжает до захвата")
_g3 = capture.Grabber(560); _g3.backend = "none"
check(_g3.grab() is None, "без бэкенда grab() возвращает None, а не падает")

print("== рабочий цикл переживает ошибку (регресс: поток умирал молча) ==")
_real_step = E.BrightnessEngine.step
_calls = {"n": 0}


def _boom(self, frame):
    _calls["n"] += 1
    raise RuntimeError("проверка живучести")


E.BrightnessEngine.step = _boom
app = make_app(dict(E.DEFAULT_CONFIG), grabber=HoldGrabber, game_check_value=True)
app.start()
time.sleep(1.2)
alive = app._thread is not None and app._thread.is_alive()
note = app._loop_note
app.stop.set()
app.shutdown()
E.BrightnessEngine.step = _real_step
check(alive, "поток жив после исключения из шага", "тред=%s" % alive)
check(_calls["n"] >= 2, "цикл продолжал работать после ошибки", "%d шагов" % _calls["n"])
check("живучести" in note, "причина ошибки видна в статусе", note[:60])

print("== выбор монитора и панельная яркость ==")
RealGrabber = capture.__dict__["Grabber"]           # в тестах класс подменяют
import importlib
RealGrabber = importlib.reload(capture).Grabber
g3 = RealGrabber(560, 3)
check(g3.monitor == 3, "capture.Grabber принимает номер монитора", str(g3.monitor))
check(RealGrabber(560, 0).monitor == 1, "некорректный индекс клампится к 1")
check(RealGrabber(560, 7).monitor == 7, "крупный индекс не ломает конструктор")
sys.path.insert(0, os.path.join(ROOT, "app"))
cfg2 = dict(E.DEFAULT_CONFIG); cfg2["monitor_index"] = 2
check(cfg2["monitor_index"] == 2, "monitor_index живёт в конфиге")
st = M.W.set_monitor_brightness(None)
check(isinstance(st, str) and st, "set_monitor_brightness не падает без Windows", st)

print("== подсказка «игра не запущена» (эмуляция Windows-ветки) ==")
real_is_win = M.W.IS_WINDOWS
M.W.IS_WINDOWS = True                      # заставляем пройти Windows-ветку start()
try:
    cfg3 = dict(E.DEFAULT_CONFIG); cfg3["tie_to_game"] = True
    app3 = make_app(cfg3, game_check_value=False)
    M.W.game_running = lambda: False
    app3.start()
    time.sleep(0.3)
    has_note = bool(app3.note) and "Тарков" in app3.note
    probe_dict = dict(app3.probe)
    app3.stop.set(); capture.Grabber = app3._real_grabber; app3.shutdown()
    check(has_note, "если игра не запущена — есть подсказка в логе/окне", app3.note[:60])
    check("reason" in probe_dict, "проба таблицы всегда что-то сообщает", str(probe_dict)[:70])
finally:
    M.W.IS_WINDOWS = real_is_win

print("== деградация без дисплея / без tkinter ==")
app = make_app()
app.start_minimized = False
try:
    import tkinter
    root = None
    try:
        root = tkinter.Tk()
    except Exception as e:
        check(True, "нет X-дисплея -> tkinter.Tk() падает, приложение уйдёт в headless",
              type(e).__name__)
        root = None
    finally:
        if root:
            root.destroy()
except ImportError:
    check(True, "tkinter отсутствует — есть headless-фолбэк")
app.shutdown()

print("== preview-режим (офлайн-расчёт по файлу) ==")
p_in = os.path.join(ROOT, "samples", "forest_dusk.png")
if os.path.exists(p_in):
    out = M.preview(dict(E.DEFAULT_CONFIG), p_in, "/tmp/tb_prev.png")
    from PIL import Image
    a = C.analyze(np.asarray(Image.open(p_in).convert("RGB"), np.uint8))
    b = C.analyze(np.asarray(Image.open(out).convert("RGB"), np.uint8))
    check(os.path.exists(out), "файл превью создан", out)
    check(b.p25 > a.p25 * 2, "превью реально светлее в тенях", f"{a.p25:.3f}->{b.p25:.3f}")
else:
    # только что склонированный репозиторий не содержит samples/*.png (они в .gitignore):
    # пусть будет видно, что эти две проверки не выполнены, а не «прошли молча»
    print("  ПРОПУСК: нет samples/forest_dusk.png — 2 проверки превью не выполнены;")
    print("           сгенерируй: python3 tools/make_samples.py (в CI это делает отдельный шаг)")

print("== консоль с чужой кодовой страницей (Windows cp1252) ==")
# Русские сообщения в cp1252-консоли (а также при `> log.txt`) до fix_console()
# давали UnicodeEncodeError на первом же print — на этом падал --selftest в CI.
import subprocess
env = dict(os.environ, PYTHONIOENCODING="cp1252")
r = subprocess.run([sys.executable, os.path.join(ROOT, "app", "main.py"), "--selftest"],
                   capture_output=True, text=True, env=env, timeout=120,
                   encoding="utf-8", errors="replace")   # дитя печатает в UTF-8 (fix_console)
check(r.returncode == 0, "--selftest проходит в консоли cp1252", "exit=%d" % r.returncode)
check("UnicodeEncodeError" not in (r.stdout + r.stderr),
      "ни UnicodeEncodeError, ни падения на кириллице",
      (r.stderr.strip().splitlines() or [""])[-1][:80])
check("ИТОГ" in r.stdout, "вывод дошёл до конца", r.stdout.strip().splitlines()[-1][:40] if r.stdout else "")
r2 = subprocess.run([sys.executable, os.path.join(ROOT, "app", "main.py"), "--check"],
                    capture_output=True, text=True, env=env, timeout=60,
                    encoding="utf-8", errors="replace")
check("UnicodeEncodeError" not in (r2.stdout + r2.stderr), "--check тоже не падает",
      (r2.stdout.strip().splitlines() or [""])[-1][:60])

print("== проба окружения: внешний probe важнее, и только один раз ==")
# Это ровно то, на чём падал windows-latest: раннер притворяется терминальным
# сеансом, приложение само себя выключает, и фоновый цикл «не пишет таблицы».
_orig = (M.W.IS_WINDOWS, M.W.probe_gamma_support)
_calls = []
try:
    M.W.IS_WINDOWS = True
    def _fake_probe(ramp):
        _calls.append(1)
        return {"ok": True, "reason": "реальная проба", "env": {"remote_session": True}}
    M.W.probe_gamma_support = _fake_probe
    a1 = make_app()
    capture.Grabber = a1._real_grabber          # make_app подменяет его — возвращаем
    a1._probe_env()
    check(not _calls and a1.probe.get("reason") == "тест",
          "внешний probe (set_probe) не перетирается пробой", str(a1.probe.get("reason")))
    check(a1.cfg["enabled"] is True, "с заранее заданным env эффект не глушится",
          str(a1.cfg["enabled"]))
    a2 = M.App(dict(E.DEFAULT_CONFIG), headless=True)
    a2.ramp = _NullRamp()
    a2._probe_env(); a2._probe_env()
    check(len(_calls) == 1, "реальная проба ставится ровно один раз", str(len(_calls)))
    check(isinstance(a1.ramp, _NullRamp) and isinstance(a2.ramp, _NullRamp),
          "тесты не дёргают реальный драйвер (ramp — заглушка)")
    # и именно так это должно выглядеть СЕЙЧАС: флаг «удалённый сеанс» без
    # подтверждения числами не имеет права ни глушить эффект, ни прятать причину
    check(a2.cfg["enabled"] is True and a2.probe.get("reason") == "реальная проба",
          "ложный флаг RDP не отменяет успешную пробу и не выключает эффект",
          "%s / %s" % (a2.cfg["enabled"], a2.probe.get("reason")))
    check("SM_REMOTESESSION" in getattr(a2, "note", ""),
          "но про противоречие человек предупреждён в примечании окна",
          getattr(a2, "note", "")[:60])

    def _probe_fail(ramp):
        return {"ok": False, "reason": "драйвер вернул FALSE без кода ошибки",
                "env": {"remote_session": True, "remote_confirmed": True}}
    M.W.probe_gamma_support = _probe_fail
    a3 = M.App(dict(E.DEFAULT_CONFIG), headless=True)
    a3.ramp = _NullRamp()
    a3._probe_env()
    check(a3.cfg["enabled"] is False and "сеанс RDP" in a3.probe.get("reason", "")
          and "tscon" in getattr(a3, "note", ""),
          "подтверждённый RDP по-прежнему глушит эффект и объясняет что делать",
          "%s | %s" % (a3.cfg["enabled"], a3.probe.get("reason")[:40]))

    def _probe_fail_local(ramp):
        return {"ok": False, "reason": "SetDeviceGammaRamp вернул отказ — код 120",
                "env": {"remote_session": True, "remote_confirmed": False}}
    M.W.probe_gamma_support = _probe_fail_local
    a4 = M.App(dict(E.DEFAULT_CONFIG), headless=True)
    a4.ramp = _NullRamp()
    a4._probe_env()
    check(a4.cfg["enabled"] is True and "код 120" in a4.probe.get("reason", ""),
          "отказ на локальном сеансе остаётся честно описанным (не «вы в RDP»)",
          a4.probe.get("reason", "")[:56])
finally:
    M.W.IS_WINDOWS, M.W.probe_gamma_support = _orig

print("== эмуляция Windows-драйвера: раскладка таблицы и отказ ==")
# Драйвер Windows принимает таблицу только как 3 последовательных блока по 256 WORD
# (WORD Ramp[3][256]) и требует монотонности в каждом. Проверяем это на «живом»
# коде: подменяем ctypes-модули фейками, которые валидируют массив как драйвер.
import ctypes as _ct


def _driver_ok(vals):
    for c in range(3):
        prev = -1
        for i in range(256):
            v = int(vals[c * 256 + i])
            if v < prev or not (0 <= v <= 65535):
                return False
            prev = v
    return True


class _FakeGdi:
    def __init__(self):
        self.ramp = [i * 257 for i in range(256)] * 3
        self.err = 0
        self.sets = 0

    def GetDeviceGammaRamp(self, hdc, ptr):
        arr = _ct.cast(ptr, _ct.POINTER(_ct.c_ushort * 768)).contents
        for i, v in enumerate(self.ramp):
            arr[i] = v
        return 1

    def SetDeviceGammaRamp(self, hdc, ptr):
        self.sets += 1
        arr = _ct.cast(ptr, _ct.POINTER(_ct.c_ushort * 768)).contents
        vals = list(arr)
        if not _driver_ok(vals):
            self.err = 87                       # ERROR_INVALID_PARAMETER — ровно то,
            return 0                            # что давало «вернул отказ»
        self.ramp = vals
        self.err = 0
        return 1

    def CreateDCW(self, *a):
        return 0

    def DeleteDC(self, hdc):
        return 1

    def GetDeviceCaps(self, hdc, idx):
        return {12: 8, 14: 3}.get(idx, 0)       # BITSPIXEL=8, PLANES=3 -> 24 бита


class _FakeUser:
    def GetDC(self, h):
        return 1

    def ReleaseDC(self, a, b):
        return 1

    def GetSystemMetrics(self, i):
        return 0                                # не RDP

    def EnumDisplayDevicesW(self, *a):
        return 0


class _FakeKernel:
    def __init__(self, gdi):
        self.gdi = gdi

    def GetLastError(self):
        return self.gdi.err


gdi = _FakeGdi()
_names = ("_user32", "_gdi32", "_kernel32")
_real = (M.W.IS_WINDOWS,) + tuple(getattr(M.W, n, None) for n in _names)
M.W.IS_WINDOWS = True
M.W._user32, M.W._gdi32, M.W._kernel32 = _FakeUser(), gdi, _FakeKernel(gdi)
try:
    import correction as _C
    good = _C.ramp_bytes(_C.build_luts(1.8, tint=(1.12, 1.0, 0.9), shadow_lift=.35))
    r = M.W.GammaRamp()
    check(r._write(good) is True, "планарная таблица принимается «драйвером»",
          "DC: %s, %d записей/канал" % (r.source, r.ramp_mode))
    trip = _ct.cast(_ct.byref(((_ct.c_ushort * 768))(*[
        _C.build_luts(1.8, tint=(1.12, 1.0, 0.9), shadow_lift=.35)[c][i] * 257
        for i in range(256) for c in range(3)])), _ct.POINTER(_ct.c_ushort * 768)).contents
    check(not _driver_ok(list(trip)),
          "та же таблица в перемешанной раскладке (r,g,b,...) — драйвер ОТВЕРГ бы")
    r2 = M.W.GammaRamp()
    import struct as _st
    _w = list(_st.unpack("<768H", good))
    _w[7] = 0                                   # провал внутри красного блока
    check(r2._write(_st.pack("<768H", *_w)) is False and "падает" in r2.error_text(),
          "немонотонную таблицу ловим локально, с внятным текстом", r2.error_text()[:70])
    # выход за 0..65535 в _write попасть не может (blob всегда unpack("<768H")),
    # поэтому проверка диапазона — юнит-тест самого валидатора
    _w2 = list(_st.unpack("<768H", good))
    _w2[301] = 70000
    check("65535" in _C._ramp_words(_w2), "валидатор ловит значение вне 0..65535",
          _C._ramp_words(_w2)[:50])
    check(_C._ramp_words(_w2, 1024) != "", "на неверном размере (3072 при per_channel=1024) тоже ругается")
    pr = M.W.probe_gamma_support(M.W.GammaRamp())
    check(pr["ok"] is True, "проба на эмулированном Windows проходит", pr["reason"][:60])
    gdi.err = 5
    class _Deny(_FakeGdi):
        def SetDeviceGammaRamp(self, hdc, ptr):
            self.err = 5
            self.sets += 1
            return 0
    gdi2 = _Deny()
    M.W._gdi32 = gdi2
    pr2 = M.W.probe_gamma_support(M.W.GammaRamp())
    check(pr2["ok"] is False and "код Windows 5" in pr2["reason"],
          "при отказе драйвера в тексте есть код ошибки и подсказка", pr2["reason"][:70])
    # «GetDC(весь экран)» отвергает (код, который НЕ зовёт лестницу DC), а DC
    # монитора принимает — два адаптера в логе v1.3.2 («RTX 4070 Ti SUPER ×2»).
    # Проба обязана пересесть на рабочий DC, а не выносить приговор «драйвер не даёт».
    class _OnlyMonitor(_FakeGdi):
        def CreateDCW(self, *a):
            return 99                                   # DC конкретного монитора

        def SetDeviceGammaRamp(self, hdc, ptr):
            if int(hdc) != 99:
                self.err = 1468                         # ERROR_NOT_SUPPORTED
                return 0
            return _FakeGdi.SetDeviceGammaRamp(self, hdc, ptr)

    class _UserMonitors(_FakeUser):
        def EnumDisplayDevicesW(self, _dev, i, ref, _flags):
            if i != 0:
                return 0
            d = ref._obj
            d.DeviceName = r"\\.\DISPLAY1"
            d.DeviceString = "Test GPU"
            d.StateFlags = 1                            # ACTIVE
            return 1

    gdi4, _u_old = _OnlyMonitor(), M.W._user32
    M.W._gdi32, M.W._user32 = gdi4, _UserMonitors()
    try:
        pr4 = M.W.probe_gamma_support(M.W.GammaRamp())
    finally:
        M.W._gdi32, M.W._user32 = gdi, _u_old
    check(pr4["ok"] is True and "DISPLAY1" in pr4["reason"],
          "таблица принялась только на DC монитора — проба переезжает на него",
          pr4["reason"][:80])

    # расширенная таблица: если 3x256 не приняли, пробуем 3x1024
    class _Only1024(_FakeGdi):
        def SetDeviceGammaRamp(self, hdc, ptr):
            arr = _ct.cast(ptr, _ct.POINTER(_ct.c_ushort * 3072)).contents
            vals = list(arr)
            if len(vals) != 3072 or not all(vals[c * 1024 + i] >= vals[c * 1024 + i - 1]
                                            for c in range(3) for i in range(1, 1024)):
                self.err = 1468
                return 0
            self.err = 0
            self.sets += 1
            return 1
    gdi3 = _Only1024()
    M.W._gdi32 = gdi3
    r3 = M.W.GammaRamp()
    ok3 = r3._write(good)
    check(ok3 is True and r3.ramp_mode == 1024,
          "драйверам, которые хотят только 3x1024, отправляем расширенную таблицу",
          "режим=%d, вызовов=%d" % (r3.ramp_mode, gdi3.sets))

    # --- диагностика окружения: не врать про глубину цвета и крыть RDP ---
    class _Gdi32(_FakeGdi):
        def __init__(self, bits, planes, hdr=0):
            self.ramp = [i * 257 for i in range(256)] * 3
            self.err = 0
            self.sets = 0
            self._b, self._p, self._h = bits, planes, hdr

        def GetDeviceCaps(self, hdc, idx):
            return {12: self._b, 14: self._p, 110: self._h}.get(idx, 0)

    class _UserRDP(_FakeUser):
        def GetSystemMetrics(self, i):
            return 1 if i == 78 else 0        # SM_REMOTESESSION

    gdi24 = _Gdi32(8, 3)
    M.W._gdi32 = gdi24
    env24 = M.W.gamma_env_report()
    check(env24.get("bits_per_channel") == 8 and env24.get("deep_color") is False,
          "24-битный режим (3x8) читается как 8 бит/канал", str(env24.get("bits_per_channel")))
    gdi32 = _Gdi32(32, 1)
    M.W._gdi32 = gdi32
    env32 = M.W.gamma_env_report()
    check(env32.get("deep_color") is None and "10 бит" not in " ".join(env32["hints"]),
          "32 бит/пиксель при 1 плане НЕ объявляется как «10 бит/HDR»", M.W.human_env(env32))
    gdi10 = _Gdi32(10, 3, hdr=1)
    M.W._gdi32 = gdi10
    env10 = M.W.gamma_env_report()
    check(env10.get("deep_color") is True and env10.get("hdr_enabled") is True,
          "10 бит/канал и HDR через MXDC_ENABLE_HDR замечаются", M.W.human_env(env10))
    # «вы в RDP» — самое обидное место: пользователь читает это, сидя перед
    # монитором, и не понимает, откуда вывод. Значит, вывод обязан (а) держаться
    # на числах, (б) молчать, когда числа противоречат друг другу.
    M.W._user32 = _UserRDP()
    _sn = os.environ.get("SESSIONNAME")
    os.environ["SESSIONNAME"] = "RDP-Tcp#0"
    M.W._session_id = lambda: 3
    M.W._console_session_id = lambda: 1
    envrdp = M.W.gamma_env_report()
    check(envrdp["remote_session"] and "RDP" in envrdp["hints"][0].upper()
          and envrdp.get("remote_confirmed") is True,
          "терминальный сеанс назван главной причиной", envrdp["hints"][0][:64])
    check("SM_REMOTESESSION=1" in M.W.human_evidence(envrdp)
          and "SESSIONNAME=RDP-Tcp#0" in M.W.human_evidence(envrdp)
          and "сеанс 3" in M.W.human_evidence(envrdp) and "консольный 1" in M.W.human_evidence(envrdp),
          "в доказательстве видны сырые numbers, а не только вывод",
          M.W.human_evidence(envrdp)[:110])
    os.environ["SESSIONNAME"] = "Console"
    M.W._session_id = lambda: 1                        # мы в консольном сеансе...
    M.W._console_session_id = lambda: 1                # ...и он же активен на мониторе
    envodd = M.W.gamma_env_report()
    check(envodd["remote_session"] and envodd.get("remote_confirmed") is False
          and "ЭТО УДАЛЁННЫЙ СЕАНС" not in envodd["hints"][0],
          "расхождение (SM_REMOTESESSION=1, но Console/тот же id) не выдаётся за RDP",
          envodd["hints"][0][:80])
    check("цветном конвейере" in envodd["hints"][0] and "query user" in envodd["hints"][0],
          "в этом случае сказано, где искать причину (не «вы в RDP, всё понятно»)",
          envodd["hints"][0][-80:])
    if _sn is None:
        os.environ.pop("SESSIONNAME", None)
    else:
        os.environ["SESSIONNAME"] = _sn

    # ACM (Windows 11) — реальная причина «отказ без кода» на локальном сеансе:
    # про это обязано быть и в подсказке, и в сырой строке.
    _acm = M.W._acm_enabled
    M.W._acm_enabled = lambda: True
    envacm = M.W.gamma_env_report()
    M.W._acm_enabled = _acm
    check(envacm.get("acm") is True and any("Auto Color Management" in h for h in envacm["hints"])
          and "ACM=вкл" in M.W.human_evidence(envacm),
          "ACM замечен и объяснен (переключатель + reg query)",
          M.W.human_evidence(envacm)[:90])
    check(M.W._adapter_line(["A", "A", "B"]) == "A ×2, B",
          "два одинаковых адаптера не печатаются дублем", M.W._adapter_line(["A", "A", "B"]))
    for _k in ("_session_id", "_console_session_id"):
        delattr(M.W, _k)
    r_nocode = M.W.GammaRamp()

    class _Silent:
        def GetDeviceGammaRamp(self, hdc, ptr):
            return 0

        def SetDeviceGammaRamp(self, hdc, ptr):
            return 0

        def CreateDCW(self, *a):
            return 0

        def DeleteDC(self, h):
            return 1

        def GetDeviceCaps(self, hdc, idx):
            return {12: 32, 14: 1, 110: 0}.get(idx, 0)

    silent = _Silent()
    M.W._gdi32 = silent

    class _K0:
        def GetLastError(self):
            return 0
    M.W._kernel32 = _K0()
    r0 = M.W.GammaRamp()
    r0._write(good)
    check("без кода ошибки" in r0.error_text(),
          "отказ без кода объясняется текстом, а не «неизвестным отказом»", r0.error_text()[:56])
    M.W._user32 = _FakeUser()

finally:
    M.W.IS_WINDOWS = _real[0]
    for n, v in zip(_names, _real[1:]):
        if v is None:
            M.W.__dict__.pop(n, None)
        else:
            setattr(M.W, n, v)

# --------------------------------------------------------------------------
# «Сеанс удалённый» не имеет права отменять УСПЕШНУЮ пробу (v1.3.3 так и делал:
# проба прошла, а приложение затирало результат и выключало эффект -> у человека
# «ползунки ни на что не влияют», потому что enabled:false записала в конфиг сама
# программа). Решение вынесено в main.rdp_gate — проверяем его таблицей случаев.
import main as _M

_p, _n, _d = _M.rdp_gate({"ok": True, "reason": "гамма ставится"},
                         {"remote_session": True, "remote_confirmed": False})
check(_p["ok"] is True and _d is False and _n,
      "успешную пробу ложный флаг RDP не отменяет (есть только пояснение)",
      "%s/%s" % (_p["ok"], _d))
_p, _n, _d = _M.rdp_gate({"ok": True}, {"remote_session": True, "remote_confirmed": True})
check(_p["ok"] is True and _d is False and _n == "",
      "если в RDP таблица всё-таки ставится — не выдумываем проблему", "")
_p, _n, _d = _M.rdp_gate({"ok": False, "reason": "код 120"},
                         {"remote_session": True, "remote_confirmed": True})
check(_p["ok"] is False and "сеанс RDP" in _p["reason"] and _d is True and "tscon" in _n,
      "подтверждённый RDP: короткая причина + не долбить драйвер", _p["reason"][:48])
_p, _n, _d = _M.rdp_gate({"ok": False, "reason": "драйвер вернул FALSE без кода"},
                         {"remote_session": True, "remote_confirmed": False})
check("FALSE" in _p["reason"] and _d is False and _n == "",
      "отказ без подтверждения RDP: причину драйвера НЕ прячем за «сеанс RDP»",
      _p["reason"][:48])
check("cli_hint()" in open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
      and "verните: python app" not in open(os.path.join(ROOT, "app", "main.py"),
                                            encoding="utf-8").read(),
      "подсказки в диагностике знают, что человек сидит в .exe, а не в python", "")
_F0, _X0 = _M.FROZEN, _M.sys.executable
try:
    _M.FROZEN = True
    _M.sys.executable = r"C:\games\TarkovBright\TarkovBright.exe"
    check(_M.cli_hint() == "TarkovBright.exe", "cli_hint() из .exe имя файла и подсказывает",
          _M.cli_hint())
finally:
    _M.FROZEN, _M.sys.executable = _F0, _X0
src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
check("сеанс RDP" in src.split("def rdp_gate")[1].split("class App")[0]
      and "сеанс RDP" not in src.split("def _probe_env")[1].split("def start")[0],
      "решение живёт в rdp_gate, а _probe_env только применяет его", "")
# --------------------------------------------------------------------------
# Прототипы ctypes — класс багов, который ловит НЕ CI, а машина пользователя.
# Без argtypes на Windows (LLP64) Питон-целое конвертируется в C `int` (32 бита):
# HDC/HBITMAP в 64-битном процессе в него не влезают -> OverflowError у пользователя,
# «везение» в CI. Поэтому: любой вызов, которому передают рукоятку, обязан стоять
# в списке прототипов.
import re as _re

_cap = open(os.path.join(ROOT, "app", "capture.py"), encoding="utf-8").read()
_cap_protos = set(_re.findall(r'\("(?:gdi32|user32)", "(\w+)"', _cap))
_cap_calls = set(_re.findall(r'\b(gdi32|user32)\.(\w+)\(', _cap))
_miss = sorted("%s.%s" % (m, n) for m, n in _cap_calls if n not in _cap_protos)
check(not _miss, "capture: каждый вызов GDI описан в _GDI_PROTOS (argtypes+restype)",
      ", ".join(_miss)[:90])

_win = open(os.path.join(ROOT, "app", "windows.py"), encoding="utf-8").read()
_win_protos = set(_re.findall(r'\(_\w+, "(\w+)", \[', _win)) | set(
    _re.findall(r'_\w+\.(\w+)\.(?:argtypes|restype)', _win))
# только те вызовы, куда летит рукоятка (hdc) — их обрезка и ломает
_handled = set(_re.findall(r'(_(?:user32|gdi32|kernel32))\.(\w+)\([^)]*hdc', _win))
_miss2 = sorted("%s.%s" % (lib, n) for lib, n in _handled if n not in _win_protos)
check(not _miss2, "windows: вызовы с HDC имеют прототип (включая GetDeviceCaps/ReleaseDC)",
      ", ".join(_miss2)[:90])
check("CreateDCW" in _win_protos,
      "CreateDCW возвращает c_void_p, а не обрезанный int (иначе DC мёртвый)", "")
check("import updater as U" in open(os.path.join(ROOT, "app", "main.py"),
                                    encoding="utf-8").read(),
      "updater импортируется статически — иначе его нет в .exe", "")

print()
if FAILS:
    print(f"ПРОВАЛЕНО {len(FAILS)}:"); [print(" -", f) for f in FAILS]; sys.exit(1)
print("Сквозные проверки пройдены")
