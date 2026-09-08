"""Собирает наглядные сравнения до/после из текущих сцен и текущего кода.

    python3 tools/make_comparison.py

Пишет samples/after/<scene>.png и пересобирает samples/before_after.png|.jpg.
Нужен Pillow; numpy — если установлен (иначе LUT применяются чистым Python'ом).
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, HERE)

from PIL import Image                                    # noqa: E402

import correction                                        # noqa: E402
import windows as W                                          # noqa: E402

W.fix_console()        # русские подписи в cp1252-консоли иначе роняют скрипт
import engine as E                                       # noqa: E402
import make_samples as MS                                # noqa: E402

OUT = os.path.join(ROOT, "samples")


def apply_pure(img, luts):
    """Коррекция таблицей без numpy: три палитры по 256 значений."""
    return Image.merge("RGB", [ch.point(list(luts[c])) for c, ch in enumerate(img.split())])


def main():
    os.makedirs(os.path.join(OUT, "after"), exist_ok=True)
    tiles = []
    for name in MS.SCENES:
        src = os.path.join(OUT, f"{name}.png")
        img = Image.open(src).convert("RGB")
        eng = E.BrightnessEngine(dict(E.DEFAULT_CONFIG), sink=None)
        if correction.HAVE_NUMPY:
            import numpy as np
            src = np.asarray(img, dtype=np.uint8)
        else:
            import capture as Cp
            src = Cp.Frame(img.tobytes("raw", "RGB"), img.width, img.height,
                           stride=img.width * 3, bpp=3, order=(0, 1, 2))
        for _ in range(60):
            info = eng.step(src)
        luts = info["luts"]
        if correction.HAVE_NUMPY:
            import numpy as np
            after = Image.fromarray(correction.apply_luts(src, luts))
        else:
            after = apply_pure(img, luts)
        after.save(os.path.join(OUT, "after", f"{name}.png"))
        tiles.append((name, img, after, info["gamma"]))
        print(f"  {name:16s} γ={info['gamma']:.2f}  p25 {info['stats'].p25:.3f}")

    w, h = tiles[0][1].size
    pad, lab = 12, 22
    sheet = Image.new("RGB", (w * 2 + pad * 3, (h + lab) * len(tiles) + pad), (18, 18, 20))
    from PIL import ImageDraw
    d = ImageDraw.Draw(sheet)
    for i, (name, before, after, gamma) in enumerate(tiles):
        y = pad + i * (h + lab)
        sheet.paste(before, (pad, y + lab))
        sheet.paste(after, (pad + w + pad, y + lab))
        d.text((pad, y), f"{name} — до", fill=(200, 200, 200))
        d.text((pad + w + pad, y), f"{name} — после (авто, γ={gamma:.2f})", fill=(150, 220, 150))
    sheet.save(os.path.join(OUT, "before_after.png"))
    # jpg — тот, что показан в README: держим его маленьким (png игнорируется git)
    small = sheet.resize((1360, int(sheet.height * 1360 / sheet.width)), Image.LANCZOS)
    small.convert("RGB").save(os.path.join(OUT, "before_after.jpg"),
                              quality=72, optimize=True, progressive=True)
    print(f"  -> {os.path.relpath(os.path.join(OUT, 'before_after.png'), ROOT)}  {sheet.size}")


if __name__ == "__main__":
    main()
