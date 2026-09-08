"""python3 tests/test_engine.py  — проверяет ядро на синтетике, без Windows."""
from __future__ import annotations
import copy, os, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import correction as C          # noqa: E402
import engine as E              # noqa: E402
import make_samples as MS       # noqa: E402

FAILS = []
def check(cond, msg, extra=""):
    print(("  ok   " if cond else "  FAIL ") + msg + (f"   [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(msg)

print("== 1. LUT: идентичность, монотонность, допустимый формат ==")
idt = C.build_luts()
for c in range(3):
    check(np.array_equal(idt[c], np.arange(256, dtype=np.uint16)), f"канал {c}: f=1 => 1:1",
          f"{idt[c][16]},{idt[c][128]},{idt[c][255]}")
blob = C.ramp_bytes(idt)
check(len(blob) == 1536, "размер ramp = 1536 байт", str(len(blob)))
words = np.frombuffer(blob, dtype="<u2")
check(words.max() <= 65535, "значения в диапазоне GDI 0..65535", str(words.max()))
# планарно: reshape(3, 256), а НЕ (256, 3) — на identity эти две раскладки
# совпадают пословно, так что старая версия проверки ничего не проверяла
check(np.array_equal(words.reshape(3, 256),
                     np.tile((np.arange(256) * 257).astype("<u2"), (3, 1))),
      "нейтральная таблица = 3 блока по 256 WORD (R, G, B) со масштабом 257")
check(C.ramp_bytes(idt) == C.identity_ramp(), "и она же == identity_ramp()")
check(C.ramp_bytes(C.build_luts()) == C.identity_ramp(), "ramp_bytes(1.0) == identity")

print("== 1b. Раскладка gamma-таблицы (её перепутать — значит получить отказ драйвера) ==")
# Windows ждёт C-шный WORD Ramp[3][256]: сначала ВСЕ красные, потом зелёные, потом синие.
red_only = [list(range(256)), [0] * 256, [0] * 256]
wred = np.frombuffer(C.ramp_bytes(red_only), dtype="<u2")
check(bool((wred[:256] == np.arange(256) * 257).all()) and bool((wred[256:] == 0).all()),
      "планарно: меняются только слова 0..255 (красный блок)")
trip = np.array([red_only[c][i] * 257 for i in range(256) for c in range(3)], dtype="<u2")
check(not bool((trip[:256] == wred[:256]).all()), "перемешанная раскладка (r,g,b,r,g,b) — ДРУГОЙ массив")
# ровно за такую немонотонность драйвер и отвечает ERROR_INVALID_PARAMETER
def monotone_by_blocks(v, n=256):
    return all(v[c * n + i] >= v[c * n + i - 1] for c in range(3) for i in range(1, n))
check(monotone_by_blocks(wred.tolist()), "наша таблица монотонна в каждом из 3 блоков")
check(not monotone_by_blocks(trip.tolist()), "перемешанная таблица немонотонна -> отказ SetDeviceGammaRamp")
check(C._ramp_words(wred.tolist()) == "", "локальная проверка пропускает корректную таблицу")
check(C._ramp_words([0, 5, 1] + [0] * 765) != "", "локальная проверка ловит немонотонность")
bad_hi = list(wred.tolist()); bad_hi[300] = 70000
check(C._ramp_words(bad_hi) != "", "локальная проверка ловит выход за 0..65535")
# и на реальной коррекции с тилтом (там каналы различаются)
tinted = C.build_luts(1.9, tint=(1.15, 1.0, 0.9), shadow_lift=.4, saturation=1.2, knee=.35)
wt = np.frombuffer(C.ramp_bytes(tinted), dtype="<u2")
check(monotone_by_blocks(wt.tolist()), "таблица с тилтом/насыщением монотонна по блокам")
check(all(wt[c * 256 + i] == tinted[c][i] * 257 for c in range(3) for i in (0, 17, 128, 255)),
      "слова таблицы = уровни LUT * 257 по своему блоку")


strong = C.build_luts(1.9, shadow_lift=.5, saturation=1.3, contrast=1.2, knee=.6,
                      tint=(1.2, 1.0, .85), clamp_floor=4)
mono = all(bool(np.all(np.diff(strong[c]) >= 0)) for c in range(3))
check(mono, "сильная коррекция остаётся монотонной (без инверсий тонов)")
check(all(max(l) <= 255 and min(l) >= 0 for l in strong), "уровни в 0..255")
check(all(l[255] >= 250 for l in strong), "белый не убивается (knee отнормирован)",
      str([int(l[255]) for l in strong]))

print("== 2. Авто-гамма по статистике ==")
dark = MS.scene_forest_dusk(); bright = MS.scene_labs(); flash = MS.scene_night_flash()
sd, sb, sf = C.analyze(dark), C.analyze(bright), C.analyze(flash)
f_dark = C.auto_gamma_factor(sd, 0.30, 1.0, 0.80, 2.0)
f_bright = C.auto_gamma_factor(sb, 0.30, 1.0, 0.80, 2.0)
check(f_dark > 1.5, "тёмный лес получает заметный подъём", f"{f_dark:.2f}")
check(f_dark <= 2.0 + 1e-9, "и он ограничен gamma_max", f"{f_dark:.2f}")
check(f_bright <= 1.05, "светлый Labs не осветляется дополнительно", f"{f_bright:.2f}")
f_flash = C.auto_gamma_factor(sf, .30, 1.0, .8, 2.0)
check(f_flash < f_dark, "залитый светом кадр получает меньше, чем почти чёрный",
      f"{f_flash:.2f} < {f_dark:.2f}")
check(f_flash <= 1.45, "и потолок подъёма разумный (без «выгорания в ноль»)", f"{f_flash:.2f}")
blown = np.clip(bright.astype(np.float32) * 1.9 + 70, 0, 255).astype(np.uint8)
sb2 = C.analyze(blown)
check(C.auto_gamma_factor(sb2, .30, 1.0, .8, 2.0) <= 1.0 + 1e-6,
      "пересвеченный экран (NVG/снег) НЕ осветляется дальше", f"p95={sb2.p95:.2f} f={C.auto_gamma_factor(sb2,.30,1.,.8,2.):.2f}")

out = C.apply_luts(dark, C.build_luts(gamma_factor=f_dark, shadow_lift=.3, saturation=1.12, knee=.35))
so = C.analyze(out)
check(so.p25 > 0.30 * 0.9, "тени подняты к целевому p25", f"{sd.p25:.3f} -> {so.p25:.3f}")
outf = C.apply_luts(flash, C.build_luts(gamma_factor=f_flash, shadow_lift=.3, saturation=1.12,
                                       knee=.35, contrast=1.05))
check(C.analyze(outf).clip_hi < sf.clip_hi + 0.02, "на фоне фонаря не появляется новый клиппинг",
      f"clip_hi {sf.clip_hi:.4f} -> {C.analyze(outf).clip_hi:.4f}")
check(so.median > sd.median and so.median < 0.85, "медиана поднята без выжигания",
      f"{sd.median:.3f} -> {so.median:.3f}")
check(so.p95 < 0.995, "нет массового клиппинга света", f"{so.p95:.3f} clip_hi={so.clip_hi:.3f}")

print("== 3. Анти-тилт ==")
cfg_gain_bound = 1.30          # для теста снимаем ограничителя сильнее дефолтных 1.15
rng, = np.random.default_rng(0),
tinted = (np.clip(bright.astype(np.float64) / 255 * np.array([.78, 1.0, 1.16]), 0, 1) * 255).astype(np.uint8)
st_t = C.analyze(tinted)
g = C.auto_tint(st_t, max_gain=cfg_gain_bound)
bal = C.apply_luts(tinted, C.build_luts(tint=tuple(1 + (x - 1) * 0.55 for x in g)))
spread_before = max(st_t.mean_rgb) - min(st_t.mean_rgb)
sb2 = C.analyze(bal)
spread_after = max(sb2.mean_rgb) - min(sb2.mean_rgb)
check(spread_after < spread_before, "перекос каналов уменьшается",
      f"{spread_before:.3f} -> {spread_after:.3f}, gains={tuple(round(x,2) for x in g)}")
check(max(g) <= cfg_gain_bound and min(g) >= 1 / cfg_gain_bound,
      "гейны в пределах tint_max_gain", str(tuple(round(x, 2) for x in g)))
check(abs(float(np.prod(g)) ** (1 / 3) - 1.0) < 0.02, "баланс не меняет общую яркость (геом. среднее = 1)",
      f"{float(np.prod(g))**(1/3):.4f}")
check(spread_after < spread_before * 0.85, "анти-тилт снимает перекос (>=15% на силе 0.55)",
      f"{spread_before:.3f} -> {spread_after:.3f}")
gd = C.auto_tint(sd, max_gain=cfg_gain_bound)
e_gate = E.BrightnessEngine(copy.deepcopy(E.DEFAULT_CONFIG), sink=lambda b: None)
gi = e_gate.current_params(sd)["tint"]
check(all(abs(x - 1.0) < 1e-6 for x in gi),
      "в почти полной темноте ББ не выдумывается (гейт по медиане)", str(tuple(round(x,2) for x in gi)))
neutral = C.auto_tint(C.analyze(bright), max_gain=cfg_gain_bound)
check(max(neutral) < 1.15, "на нейтральной сцене тилт почти не вмешивается",
      str(tuple(round(x, 2) for x in neutral)))

print("== 3a2. Контраст не тонет: анти-мыло ==")
c2 = copy.deepcopy(E.DEFAULT_CONFIG)
e2 = E.BrightnessEngine(c2, sink=lambda b: None)
info2 = e2.step(dark)
for _ in range(120):
    info2 = e2.step(dark)
o2 = C.analyze(C.apply_luts(dark, info2["luts"]))
check(o2.median - o2.p05 > (sd.median - sd.p05) * 1.8,
      "разброс в тенях вырос, а не схлопнулся (главное качество)",
      f"{sd.median - sd.p05:.3f} -> {o2.median - o2.p05:.3f}")
check(o2.p05 <= 0.20, "нет «серого молока» на дне кадра", f"p05={o2.p05:.3f}")
check(o2.p25 >= 0.15, "тени реально вытянуты к цели", f"p25={o2.p25:.3f}")
lut2 = info2["luts"][0]
check(int(lut2[0]) <= 3, "абсолютный чёрный остался чёрным", str(int(lut2[0])))
check(c2["contrast"] * (1 + c2["auto_contrast"] * (info2["gamma"] - 1)) > 1.15,
      "авто-контраст догоняет выкрученную гамму",
      f"contrast={c2['contrast'] * (1 + c2['auto_contrast'] * (info2['gamma'] - 1)):.3f}")
info_b = E.BrightnessEngine(copy.deepcopy(E.DEFAULT_CONFIG), sink=lambda b: None)
for _ in range(60):
    ib = info_b.step(bright)
check(ib["params"]["gamma"] <= 1.001 and ib["luts"][0][0] <= 3,
      "на светлом кадре авто не осветляет и не мелочит", f"gamma={ib['params']['gamma']:.3f}")

print("== 3b. Подъём чёрных гаснет на светлых сценах ==")
c_dark = copy.deepcopy(E.DEFAULT_CONFIG)
e_d = E.BrightnessEngine(c_dark, sink=lambda b: None)
e_d.step(dark)
lift_dark = e_d.effective_lift(C.analyze(dark))
e_b = E.BrightnessEngine(copy.deepcopy(E.DEFAULT_CONFIG), sink=lambda b: None)
e_b.step(bright)
lift_bright = e_b.effective_lift(C.analyze(bright))
check(lift_bright < lift_dark * 0.45, "в светлом Labs чёрные почти не поднимаются",
      f"лес {lift_dark:.3f} -> labs {lift_bright:.3f}")
check(lift_dark > 0, "в тёмном лесу подъём работает на полную", f"{lift_dark:.3f}")

print("== 4. Движок: сходимость и стабильность (эмуляция потока) ==")
sent = []
def sink(blob):
    sent.append(None if blob is None else np.frombuffer(blob, dtype="<u2").reshape(256, 3).copy())

cfg = E.load_config(os.devnull) if False else __import__("copy").deepcopy(E.DEFAULT_CONFIG)
eng = E.BrightnessEngine(cfg, sink=sink)
gammas = []
for i in range(60):
    eng.step(dark)
    gammas.append(eng.st.gamma)
check(abs(gammas[-1] - min(max(C.auto_gamma_factor(C.analyze(dark), cfg["target_p25"],
      cfg["auto_strength"], cfg["gamma_min"], cfg["gamma_max"]), 0), 9)) < 0.15,
      "гамма сходится к целевой за ~60 тиков", f"{gammas[0]:.2f} -> {gammas[-1]:.3f}")
jitter = max(abs(gammas[i] - gammas[i-1]) for i in range(40, 60))
check(jitter < 0.02, "в неподвижной сцене нет дрожания", f"max шаг={jitter:.4f}")
check(len(sent) > 3, "таблица действительно отправляется", f"{len(sent)} раз")
check(all(s is not None and s.shape == (256, 3) and int(s.max()) <= 65535 for s in sent),
      "отправляемые ramp валидны")

# яркая сцена: движок не должен разгонять картинку
eng2 = E.BrightnessEngine(__import__("copy").deepcopy(E.DEFAULT_CONFIG), sink=lambda b: None)
for i in range(80):
    eng2.step(bright)
check(eng2.st.gamma < 1.12, "на светлом Labs авто-гамма ~1.0 (не белит)", f"{eng2.st.gamma:.3f}")

# резкая смена сцены (Run -> Labs): проверка плавности
eng3 = E.BrightnessEngine(__import__("copy").deepcopy(E.DEFAULT_CONFIG), sink=lambda b: None)
for i in range(40): eng3.step(dark)
seq = []
for i in range(12):
    eng3.step(bright); seq.append(eng3.st.gamma)
steps = [abs(seq[i]-seq[i-1]) for i in range(1, len(seq))]
check(max(steps) < 0.35, "при вбегании в светлое гамма меняется плавно", f"max шаг={max(steps):.3f}")

print("== 5. Профили и конфиг ==")
before = dict(eng.cfg)
name = eng.next_profile()
check(name in E.PROFILES, "профили листаются", str(name))
check(eng.cfg["target_p25"] != before["target_p25"] or eng.cfg.get("auto_exposure") != before.get("auto_exposure"),
      "профиль меняет параметры")
for pname, over in E.PROFILES.items():
    c = __import__("copy").deepcopy(E.DEFAULT_CONFIG); c.update(over)
    e = E.BrightnessEngine(c, sink=lambda b: None)
    e.step(dark if "ночь" in pname or "Ручной" in pname else bright)
    ok = 0.5 <= e.st.gamma <= 3.0 and all(0.2 <= t <= 5 for t in e.st.tint)
    check(ok, f"профиль '{pname}' даёт адекватные параметры", f"gamma={e.st.gamma:.2f}")
path = E.save_config(E.DEFAULT_CONFIG, "/tmp/tb_cfg.json")
back = E.load_config(path)
check(back == E.DEFAULT_CONFIG, "config.json round-trip", path)

print("== 6. Скорость тика ==")
import time
e = E.BrightnessEngine(__import__("copy").deepcopy(E.DEFAULT_CONFIG), sink=lambda b: None)
e.step(dark)
t0 = time.perf_counter(); n = 50
for _ in range(n): e.step(dark)
ms = (time.perf_counter() - t0) / n * 1000
check(ms < 8, f"тик авто-подготовки лёгкий: {ms:.2f} мс (при 12 Гц = {ms/1000*100:.1f}% ядра)")

print()
if FAILS:
    print(f"ПРОВАЛЕНО {len(FAILS)}:"); [print(" -", f) for f in FAILS]; sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
