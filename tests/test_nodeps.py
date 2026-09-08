"""python3 tests/test_nodeps.py

Доказательство, что приложение работает БЕЗ numpy/mss/Pillow: запускаем
подпроцесс, где эти модули заблокированы, и прогоняем там весь рабочий путь —
Frame -> статистика -> авто-параметры -> LUT -> 1536 байт для SetDeviceGammaRamp.
Заодно сверяем квантили чистого пути с numpy-эталоном (тот же кадр).
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import capture                  # noqa: E402
import make_samples as MS       # noqa: E402
import engine as E              # noqa: E402

import windows as W               # noqa: E402
W.fix_console()                   # русские print не должны падать в cp1252-консоли

FAILS = []


def check(cond, msg, extra=""):
    print(("  ok   " if cond else "  FAIL ") + msg + (f"   [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(msg)


def make_frame(arr):
    """ndarray RGB -> capture.Frame в формате GDI-DIB (BGRX/BGR, stride с выравниванием)."""
    h, w = arr.shape[:2]
    row = ((w * 3 + 3) // 4) * 4
    buf = bytearray(row * h)
    bgr = np.ascontiguousarray(arr[:, :, ::-1])
    for y in range(h):
        buf[y * row: y * row + w * 3] = bgr[y].tobytes()
    return capture.Frame(bytes(buf), w, h, stride=row, bpp=3, order=(2, 1, 0))


FRAMES = {}
SCENES = {n: MS.SCENES[n]() for n in MS.SCENES}
tmpdir = tempfile.mkdtemp(prefix="tb_nodeps_")
refs = {}
CFG = dict(E.DEFAULT_CONFIG)
for name, arr in SCENES.items():
    fr = make_frame(arr)
    path = os.path.join(tmpdir, f"{name}.bin")
    with open(path, "wb") as f:
        f.write(fr.pixels)
    FRAMES[name] = dict(path=path, width=fr.width, height=fr.height, stride=fr.stride)
    # ЭТАЛОН: тот же движок, те же 60 тиков, но кадры читает numpy-путь.
    eng = E.BrightnessEngine(dict(CFG), sink=None)
    for _ in range(60):
        info = eng.step(arr)
    st = info["stats"]
    refs[name] = dict(p05=st.p05, p25=st.p25, median=st.median, p95=st.p95,
                      band=[float(x) for x in (st.band_rgb or (0, 0, 0))],
                      gamma=info["gamma"],
                      luts=[list(map(int, l)) for l in info["luts"]])

CHILD = r'''
import json, os, sys, time
BLOCK = ("numpy", "mss", "PIL")


class _Blocker:
    """Делает вид, что сторонних модулей нет: приложение обязано работать на stdlib."""
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top in BLOCK:
            raise ImportError("%s заблокирован тестом" % name)
        return None


sys.meta_path.insert(0, _Blocker())
for m in list(sys.modules):
    if m.split(".")[0] in BLOCK:
        del sys.modules[m]
try:
    import numpy                      # noqa: F401
    raise SystemExit("блок не сработал: numpy импортируется")
except ImportError:
    pass

sys.path.insert(0, sys.argv[1])
import correction as C
import capture
import engine as E

assert not C.HAVE_NUMPY, "correction должен увидеть отсутствие numpy"
res = {"checks": []}


def ck(cond, msg, extra=""):
    res["checks"].append([bool(cond), msg, str(extra)])


# --- LUT на чистом python ---
ck(C.build_luts()[0] == list(range(256)), "γ=1 даёт тождественную таблицу")
sets = [dict(), dict(gamma_factor=2.2, shadow_lift=.6, saturation=1.3, knee=.6, black_point=.006, clamp_floor=4),
        dict(gamma_factor=.85, contrast=.9, saturation=.85),
        dict(gamma_factor=1.45, tint=(1.06, .98, 1.11), shadow_lift=.3, contrast=1.2, saturation=1.15, knee=.3)]
mono = True
ident_max = 0
for kw in sets:
    l = C.build_luts(**kw)
    mono = mono and all(l[c][i] <= l[c][i + 1] for c in range(3) for i in range(255))
    ck(all(0 <= v <= 255 for v in l[0]) and len(l[0]) == 256, "уровни в 0..255", str(kw)[:40])
ck(mono, "все LUT монотонны (без 1-уровневых запинок)", "")
blob = C.ramp_bytes(C.build_luts())
ck(len(blob) == 1536, "ramp = 1536 байт", str(len(blob)))
ck(blob == C.identity_ramp(), "ramp_bytes(γ=1) == заводская таблица")
mx = max(int.from_bytes(blob[i:i + 2], "little") for i in range(0, len(blob), 2))
ck(0 < mx <= 65535, "шкала GDI 0..65535 соблюдена", str(mx))
strong = C.ramp_bytes(C.build_luts(2.0, shadow_lift=.4, saturation=1.2))
mx2 = max(int.from_bytes(strong[i:i + 2], "little") for i in range(0, len(strong), 2))
ck(mx2 <= 65535, "на сильной коррекции нет переполнения WORD", str(mx2))

# --- весь рабочий путь на Frame без numpy ---
frames = json.loads(open(sys.argv[2], encoding="utf-8").read())
refs = json.loads(sys.argv[3])
worst_q = 0.0
worst_band = 0.0
for name, meta in frames.items():
    data = open(meta["path"], "rb").read()
    fr = capture.Frame(data, meta["width"], meta["height"], stride=meta["stride"], bpp=3, order=(2, 1, 0))
    eng = E.BrightnessEngine(dict(E.DEFAULT_CONFIG), sink=None)
    t0 = time.perf_counter()
    for _ in range(60):
        info = eng.step(fr)
    per = (time.perf_counter() - t0) / 60 * 1000
    st = info["stats"]
    r = refs[name]
    dq = max(abs(st.p05 - r["p05"]), abs(st.p25 - r["p25"]), abs(st.median - r["median"]), abs(st.p95 - r["p95"]))
    worst_q = max(worst_q, dq)
    if r["band"][2]:
        worst_band = max(worst_band, max(abs(a - b) for a, b in zip(st.band_rgb or (0, 0, 0), r["band"])))
    ck(dq < 8 / 255.0, "%s: квантили pure-пути = numpy-эталон (±8 уровней)" % name, "%.2f ур." % (dq * 255))
    dl = max(max(abs(x - y) for x, y in zip(lref, lpure)) for lref, lpure in zip(r["luts"], info["luts"]))
    ck(dl <= 4, "%s: готовые LUT (весь контур) расходятся с numpy <= 4 уровней" % name, "%d ур." % dl)
    ck(1.0 <= info["gamma"] <= E.DEFAULT_CONFIG["gamma_max"] + 1e-6,
       "%s: авто-гамма в пределах" % name, "%.2f (эталон %.2f)" % (info["gamma"], r["gamma"]))
    ck(abs(info["gamma"] - r["gamma"]) < 0.12, "%s: pure и numpy сходятся по γ" % name,
       "%.2f vs %.2f" % (info["gamma"], r["gamma"]))
    ck(per < 20.0, "%s: тик без numpy быстрый" % name, "%.1f мс" % per)
    ck(bool(info["luts"]) and len(info["luts"][0]) == 256, "%s: LUT собраны" % name)

# --- движок + отправка в «устройство» ---
sent = []
eng = E.BrightnessEngine(dict(E.DEFAULT_CONFIG), sink=lambda b: sent.append(b))
meta = frames["forest_dusk"]
fr = capture.Frame(open(meta["path"], "rb").read(), meta["width"], meta["height"],
                   stride=meta["stride"], bpp=3, order=(2, 1, 0))
for _ in range(80):
    eng.step(fr)
ck(len(sent) > 2, "таблицы реально уходят в sink", str(len(sent)))
ck(all(s is not None and len(s) == 1536 for s in sent), "каждый пакет 1536 байт")
eng.restore_screen()
ck(sent[-1] is None, "restore_screen шлёт None (верни оригинал)")

# --- конфиг ---
p = E.save_config(dict(E.DEFAULT_CONFIG), os.path.join(sys.argv[4], "c.json"))
back = E.load_config(p)
ck(back == dict(E.DEFAULT_CONFIG), "config round-trip без numpy", p)

# --- windows/capture импортируются и не падают ---
import windows as W
g = capture.Grabber(560, 1)
ck(g.backend in ("none", "gdi", "mss", "pillow"), "capture выбирает доступный бэкенд", g.backend + " " + str(g.last_error)[:40])
ck(g.grab() is None or hasattr(g.grab(), "pixels"), "grab() не падает без зависимостей")
ck(W.IS_WINDOWS in (True, False), "windows.py импортируется без numpy")
ck(W.foreground_process() == "" or isinstance(W.foreground_process(), str), "поиск процесса игры не падает")

print(json.dumps(res))
sys.exit(0 if all(c[0] for c in res["checks"]) else 1)
'''

with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
    f.write(CHILD)
    child_path = f.name

env_meta = os.path.join(tmpdir, "meta.json")
with open(env_meta, "w", encoding="utf-8") as f:
    json.dump(FRAMES, f)

print("== запуск приложения в процессе без numpy / mss / Pillow ==")
r = subprocess.run([sys.executable, child_path, os.path.join(ROOT, "app"), env_meta,
                    json.dumps(refs), tmpdir], capture_output=True, text=True, timeout=180)
out = (r.stdout or "").strip()
try:
    payload = json.loads(out.splitlines()[-1]) if out else {"checks": []}
    for ok, msg, extra in payload["checks"]:
        print(("  ok   " if ok else "  FAIL ") + msg + (f"   [{extra}]" if extra else ""))
        if not ok:
            FAILS.append(msg)
    n = len(payload["checks"])
except Exception:
    n = 0
    print("  FAIL не разобрал вывод подпроцесса:", out[:400], r.stderr[:800])
if r.returncode != 0:
    check(False, "подпроцесс без numpy завершился успешно", f"code={r.returncode}")
    if r.stderr:
        print("   stderr:", r.stderr.strip()[:1500])
else:
    check(n >= 25, f"подпроцесс выполнил все {n} проверок")

print()
if FAILS:
    print(f"ПРОВАЛЕНО {len(FAILS)}:"); [print(" -", x) for x in FAILS]; sys.exit(1)
print("Приложение работает на голом Python (без зависимостей) — проверено в изолированном процессе")
