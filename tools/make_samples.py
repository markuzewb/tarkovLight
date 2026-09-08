"""Генератор тестовых кадров в духе Таркова (без скринов игры — чтобы гонять
алгоритм легально и воспроизводимо) + проверка, что авто-подстройка сходится.

Запуск:  python3 tools/make_samples.py
Тесты:   python3 tests/test_engine.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

import correction as C  # noqa: E402


def _canvas(h=540, w=960, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    return rng, yy.astype(np.float64) / h, xx.astype(np.float64) / w


def vignette(a, strength=0.85):
    h, w = a.shape[:2]
    y, x = np.mgrid[0:h, 0:w].astype(np.float64)
    r = np.sqrt(((x / w - 0.5) * 1.9) ** 2 + ((y / h - 0.5) * 1.9) ** 2)
    v = 1.0 - strength * np.clip(r - 0.35, 0, 1) ** 1.6
    return np.clip(a * v[..., None], 0, 1)


def grain(a, rng, amt=0.02):
    return np.clip(a + rng.normal(0, amt, a.shape), 0, 1)


def scene_forest_dusk(seed=1):
    """Лес на закате: тёмный, зелёно-синий тилт, много деталей в тенях."""
    rng, ny, nx = _canvas(seed=seed)
    sky = np.clip(0.30 - ny * 0.26, 0, 1)
    a = np.dstack([sky * 0.55, sky * 0.78, sky * 0.95])
    for i in range(46):                        # стволы
        cx = rng.uniform(0, 1); wd = rng.uniform(0.004, 0.02)
        dark = rng.uniform(0.02, 0.10)
        trunk = np.exp(-0.5 * ((nx - cx) / wd) ** 2)
        a = a * (1 - trunk[..., None]) + np.array([dark, dark * 1.25, dark * 0.9]) * trunk[..., None]
    for i in range(120):                       # кусты/трава снизу
        cx = rng.uniform(0, 1); cy = rng.uniform(0.55, 1.0)
        s = rng.uniform(0.02, 0.10); lv = rng.uniform(0.03, 0.14)
        blob = np.exp(-0.5 * (((nx - cx) / s) ** 2 + ((ny - cy) / (s * 1.4)) ** 2))
        a = a * (1 - blob[..., None]) + np.array([lv * 0.8, lv, lv * 0.6]) * blob[..., None]
    return (np.clip(grain(vignette(a, 0.9), rng), 0, 1) * 255).astype(np.uint8)


def scene_labs(seed=2):
    """Reserve/Labs: белый свет, светлые стены, светящиеся экраны."""
    rng, ny, nx = _canvas(seed=seed)
    a = np.dstack([np.clip(0.42 + 0.35 * np.exp(-(((nx - 0.5) ** 2) / 0.02)), 0, 1) * 0.9,
                   np.clip(0.44 + 0.35 * np.exp(-(((nx - 0.5) ** 2) / 0.02)), 0, 1) * 0.9,
                   np.clip(0.52 + 0.30 * np.exp(-(((nx - 0.5) ** 2) / 0.02)), 0, 1)])
    for i in range(6):
        cy = rng.uniform(0.1, 0.35); cx = rng.uniform(0.1, 0.9)
        lamp = np.exp(-0.5 * (((nx - cx) / 0.06) ** 2 + ((ny - cy) / 0.02) ** 2))
        a = np.clip(a + lamp[..., None] * 0.75, 0, 1)
    for i in range(20):
        cx = rng.uniform(0, 1); cy = rng.uniform(0.6, 0.95); s = rng.uniform(0.02, 0.07)
        box = (((np.abs(nx - cx) < s) & (np.abs(ny - cy) < s * 0.8))[..., None]).astype(np.float64)
        lv = rng.uniform(0.15, 0.4)
        a = a * (1 - box) + np.array([lv * 1.1, lv, lv * 0.8]) * box
    return (np.clip(grain(vignette(a, 0.6), rng, 0.015), 0, 1) * 255).astype(np.uint8)


def scene_night_flash(seed=3):
    """Ночь + фонарь в центр: тёмные края, залитый центр (проверка на клампинг)."""
    rng, ny, nx = _canvas(seed=seed)
    base = np.full((540, 960, 3), 0.035)
    r = np.sqrt((nx - 0.55) ** 2 + (ny - 0.5) ** 2)
    beam = np.clip(0.85 * np.exp(-(r / 0.28) ** 2), 0, 1)
    a = base + np.dstack([beam, beam * 0.98, beam * 0.9])
    for i in range(200):
        cx = rng.uniform(0, 1); cy = rng.uniform(0, 1); s = rng.uniform(0.01, 0.05)
        blob = np.exp(-0.5 * (((nx - cx) / s) ** 2 + ((ny - cy) / (s * 1.2)) ** 2))
        a = a + np.array([0.02, 0.035, 0.02]) * blob[..., None]
    return (np.clip(grain(vignette(a, 0.95), rng, 0.03), 0, 1) * 255).astype(np.uint8)


def scene_fog(seed=4):
    """Туман/изнанка: низкоконтрастная сине-серая муть."""
    rng, ny, nx = _canvas(seed=seed)
    fog = 0.20 + 0.10 * np.exp(-((ny - 0.55) ** 2) / 0.05)
    a = np.dstack([fog * 0.9, fog * 1.0, fog * 1.15])
    for i in range(60):
        cx = rng.uniform(0, 1); cy = rng.uniform(0.6, 1.0); s = rng.uniform(0.03, 0.12)
        blob = np.exp(-0.5 * (((nx - cx) / s) ** 2 + ((ny - cy) / (s * 0.6)) ** 2))
        a = a - 0.05 * blob[..., None]
    return (np.clip(grain(vignette(a, 0.75), rng, 0.025), 0, 1) * 255).astype(np.uint8)


SCENES = {
    "forest_dusk": scene_forest_dusk,
    "labs_bright": scene_labs,
    "night_flash": scene_night_flash,
    "fog_lowcontrast": scene_fog,
}


def save_png(path, arr):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.fromarray(arr).save(path)


if __name__ == "__main__":
    out = os.path.join(ROOT, "samples")
    for name, fn in SCENES.items():
        arr = fn()
        save_png(os.path.join(out, f"{name}.png"), arr)
        st = C.analyze(arr)
        print(f"{name:16s} p05={st.p05:.3f} p25={st.p25:.3f} med={st.median:.3f} "
              f"p95={st.p95:.3f} clip_hi={st.clip_hi:.3f} meanRGB="
              + ",".join(f"{v:.3f}" for v in st.mean_rgb))
