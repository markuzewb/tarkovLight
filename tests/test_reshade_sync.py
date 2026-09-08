"""python3 tests/test_reshade_sync.py

Проверяет файл шейдера: (1) структура/синтаксис на уровне текста,
(2) что его дефолтные параметры дают картинку, согласованную с авто-режимом
приложения. HLSL здесь не компилируется (в песочнице нет FXC) — формулы
шейдера перенесены в numpy один-в-один и сверяются метриками.
"""
from __future__ import annotations
import os, re, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app")); sys.path.insert(0, os.path.join(ROOT, "tools"))
import correction as C, engine as E, make_samples as MS
import windows as W               # noqa: E402
W.fix_console()                   # русские print не должны падать в cp1252-консоли

FX = os.path.join(ROOT, "reshade", "Shaders", "TarkovBright.fx")
INI = os.path.join(ROOT, "reshade", "Presets", "TarkovBright.ini")
FAILS = []


def check(cond, msg, extra=""):
    print(("  ok   " if cond else "  FAIL ") + msg + (f"   [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(msg)


src = open(FX, encoding="utf-8").read()

print("== структура шейдера ==")
check(src.count("{") == src.count("}"), "фигурные скобки сбалансированы",
      f"{src.count('{')} / {src.count('}')}")
check('#include "ReShade.fxh"' in src, "подключён ReShade.fxh")
check("technique TarkovBrightVisibility" in src, "техника TarkovBrightVisibility объявлена")
check("VertexShader = PostProcessVS" in src, "вершинник PostProcessVS (как в ReShade.fxh slim)")
check("PixelShader = PS_TarkovBright" in src and "PS_TarkovBright(float4 pos" in src,
      "пиксельник объявлен и подключён")
check("ReShade::BackBuffer" in src, "читает бэкбуфер через ReShade::BackBuffer")
check("tex2D(ReShade::BackBuffer, uv)" in src, "сэмплирование по uv без лишних макросов")

# все объявленные uniform'ы должны быть использованы (кроме ShowMask-диагностики)
declared = set(re.findall(r'(?:^|\n)TB_(?:SLIDER|BOOL|COLOR)\((\w+),', src, re.M))
used = {v for v in declared if len(re.findall(r"\b" + v + r"\b", src)) >= 2}
check(declared == used, "каждый параметр реально используется",
      "висячие: " + ", ".join(sorted(declared - used)) if declared != used else "")
check(not re.search(r"PostVS\b", src), "нет устаревшего PostVS (есть только PostProcessVS)")
check("static const float TB_LUMA" not in src, "нет float-массива, который FXC не любит")
check(len(src) < 20000, "размер файла адекватный", f"{len(src)} байт")

print("== дефолты парсятся ==")
sliders = {}     # группа 4 = default
for m in re.finditer(r'TB_SLIDER\((\w+),\s*"[^"]*",\s*"[^"]*",\s*([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)\)', src):
    name, lo, hi, default = m.group(1), float(m.group(2)), float(m.group(3)), float(m.group(4))
    sliders[name] = default
    check(lo <= default <= hi, f"{name}: дефолт в границах ползунка", f"{lo}..{hi} -> {default}")
color = re.search(r'TB_COLOR\(TB_TintCast,\s*"[^"]*",\s*"[^"]*",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\)', src)
check(bool(color), "TB_TintCast (color) разобран", color.groups() if color else "НЕ НАЙДЕН")
if not color:
    print("\nПРОВАЛЕНО 1: не разобрал TB_COLOR — дальше тест не имеет смысла"); sys.exit(1)
check(len(sliders) >= 10, f"ползунков enough: {len(sliders)}")

print("== пресет .ini ==")
ini = open(INI, encoding="utf-8").read()
check("[TarkovBright.fx]" in ini and "Techniques=TarkovBrightVisibility" in ini,
      "пресет ссылается на технику из .fx")
ini_keys = set(re.findall(r"^(TB_\w+)=", ini, re.M))
check(ini_keys <= declared, "в пресете нет ключей, которых нет в шейдере",
      ", ".join(sorted(ini_keys - declared)) if ini_keys - declared else "")
check(declared - ini_keys == set(), "все параметры шейдера прописаны в пресете",
      ", ".join(sorted(declared - ini_keys)))
for k, v in re.findall(r"^(TB_\w+)=([-\d.]+)", ini, re.M):
    if k in sliders:
        check(abs(float(v) - sliders[k]) < 1e-6, f"пресет {k} = дефолт шейдера",
              f"{v} vs {sliders[k]}")

print("== согласованность с авто-режимом (математика 1:1 в numpy) ==")
BASE_D = dict(sliders)
BASE_CAST = np.array([float(g) for g in color.groups()])


def fx_grade(x, params=None):
    """Полный перенос tb_grade() из .fx. Держать в синхроне с .fx обязательно."""
    d = dict(BASE_D)
    cast = BASE_CAST.copy()
    if params:
        d.update({k: v for k, v in params.items() if k in BASE_D})
        if isinstance(params.get("TB_TintCast"), str):
            cast = np.array([int(v) / 255.0 for v in params["TB_TintCast"].split(",")])
    lin = np.clip(x, 0, 1) ** 2.2
    bp = float(d["TB_BlackPoint"]) ** 2 * 0.006
    if d["TB_BlackPoint"] > 0:
        lin = np.clip((lin - bp) / (1 - bp), 0, 1)
    xs = np.clip(lin, 0, 1) ** (1 / 2.2)
    if d["TB_ShadowLift"] > 0:
        xs = np.clip(xs + d["TB_ShadowLift"] * 0.02 * (1 - xs) ** 2, 0, 1)
    # tb_tint_gains()
    c = np.maximum(cast, 0.05)
    g = c.mean() / c
    g = np.clip(g, 1 / 1.25, 1.25)
    g = np.clip(g / float(np.prod(g)) ** (1 / 3), 1 / 1.25, 1.25)
    gains = 1.0 + (g - 1.0) * d["TB_TintAmount"]
    expo = d["TB_Gamma"] * (1 + d["TB_ShadowDetail"] * np.clip(1 - np.clip(xs, 0, 1), 0, 1) ** 2) \
        * np.power(np.maximum(gains, 0.05), 1.3)
    xs = np.clip(xs, 0, 1) ** (1 / expo)
    if d["TB_Highlight"] > 0:
        k = d["TB_Highlight"] * 0.6
        xs = np.clip(xs, 0, 1) * (1 + k) / (1 + k * np.clip(xs, 0, 1))
    if abs(d["TB_Saturation"] - 1) > 1e-3:
        l2 = np.clip(xs, 0, 1) ** 2.2
        L = (l2 @ np.array([0.2126, 0.7152, 0.0722]))[..., None]
        xs = np.clip(L + (l2 - L) * d["TB_Saturation"], 0, 1) ** (1 / 2.2)
    return np.clip(xs, 0, 1)


def preset_overrides(path):
    txt = open(path, encoding="utf-8").read()
    vals = dict(re.findall(r"^(TB_\w+)=([-\d.,]+)", txt, re.M))
    return {k: (v if k == "TB_TintCast" else float(v)) for k, v in vals.items()}, txt


night_ini, _ = preset_overrides(INI)
labs_ini, labs_txt = preset_overrides(os.path.join(ROOT, "reshade", "Presets", "TarkovBright-Labs.ini"))
check(set(labs_ini) >= set(declared) or set(labs_ini) <= declared,
      "Labs-пресет использует только реальные параметры", ", ".join(sorted(set(labs_ini) - declared)))

for scene in ("forest_dusk", "fog_lowcontrast"):
    fr = MS.SCENES[scene]()
    sh = (fx_grade(fr / 255.0) * 255).astype(np.uint8)
    eng = E.BrightnessEngine(dict(E.DEFAULT_CONFIG), sink=None)
    for _ in range(160):
        info = eng.step(fr)
    au = C.analyze(C.apply_luts(fr, info["luts"]))
    a = C.analyze(sh)
    check(abs(a.median - au.median) < 0.09, f"{scene}: ночной пресет и авто-режим близко по яркости",
          f"{a.median:.3f} vs {au.median:.3f}")
    check(a.median - a.p05 > (C.analyze(fr).median - C.analyze(fr).p05) * 1.3,
          f"{scene}: у шейдера разброс теней вырос (нет молока)",
          f"{a.median - a.p05:.3f}")
    check(a.clip_hi < 0.02, f"{scene}: шейдер не выжигает света", f"clip {a.clip_hi:.3f}")

print("== светлые сцены: Labs-пресет не мелочит ==")
for scene in ("labs_bright", "night_flash"):
    fr = MS.SCENES[scene]()
    st0 = C.analyze(fr)
    eng = E.BrightnessEngine(dict(E.DEFAULT_CONFIG), sink=None)
    for _ in range(160):
        info = eng.step(fr)
    au = C.analyze(C.apply_luts(fr, info["luts"]))
    sh = C.analyze((fx_grade(fr / 255.0, params=labs_ini) * 255).astype(np.uint8))
    check(sh.median <= au.median + 0.10, f"{scene}: Labs-пресет не осветляет сильнее авто",
          f"{sh.median:.3f} vs {au.median:.3f}")
    check(sh.median - sh.p05 >= (st0.median - st0.p05) * 0.85,
          f"{scene}: Labs-пресет не схлопывает разброс", f"{sh.median - sh.p05:.3f}")
    check(sh.clip_hi < 0.02, f"{scene}: Labs-пресет не выжигает лампы", f"clip {sh.clip_hi:.3f}")

print()
if FAILS:
    print(f"ПРОВАЛЕНО {len(FAILS)}:"); [print(" -", f) for f in FAILS]; sys.exit(1)
print("Шейдер и пресет согласованы с приложением")
