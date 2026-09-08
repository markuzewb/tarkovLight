#!/usr/bin/env python3
"""Офлайн-подбор настроек по своим скриншотам (без игры и без монитора).

    python3 tools/preview.py --all                    # 4 синтетических сцены
    python3 tools/preview.py screen1.png screen2.png  # свои скриншоты (Alt+A в Таркове)
    python3 tools/preview.py --profile "Tarkov — Reserve / Labs" shot.png
    python3 tools/preview.py --gamma 1.4 shot.png     # фиксированная гамма, без авто

Пишет <имя>_bright.png рядом и печатает, что посчитал авто-блок.
To же самое умеет `python app/main.py --preview ...`.
"""
from __future__ import annotations
import argparse, copy, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import engine as E                      # noqa: E402
import make_samples as MS               # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*")
    ap.add_argument("--all", action="store_true", help="сгенерировать тестовые сцены и прогнать их")
    ap.add_argument("--profile")
    ap.add_argument("--gamma", type=float)
    ap.add_argument("--out")
    a = ap.parse_args()

    import main as M
    cfg = copy.deepcopy(E.DEFAULT_CONFIG)
    if a.profile:
        cfg["profile"] = a.profile
        cfg.update(copy.deepcopy(E.PROFILES[a.profile]))
    if a.gamma:
        cfg["auto_exposure"] = False
        cfg["manual_gamma"] = a.gamma

    paths = list(a.images)
    if a.all:
        os.makedirs(os.path.join(ROOT, "samples"), exist_ok=True)
        for name, fn in MS.SCENES.items():
            p = os.path.join(ROOT, "samples", f"{name}.png")
            MS.save_png(p, fn())
            paths.append(p)
    if not paths:
        ap.print_help()
        return 2
    for p in paths:
        M.preview(cfg, p, os.path.join(a.out, os.path.basename(p)) if a.out else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
