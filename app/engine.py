"""Движок: кадр -> статистика -> плавно посчитанная gamma-таблица в устройство.

Логика «идеальной подстройки»:
  1. Меряем не среднюю яркость (её ломают виньетка и звёзды), а квантили.
  2. Цель — держать нижние середины тона (p25) на уровне target: именно там
     в Таркове лежат кусты, углы, силуэты. Потянули p25 вверх = видно стволы.
  3. Изменения сглаживаются EMA с «мёртвой зоной»: в паузах и при беге по
     коридору картинка не пульсирует.
  4. Столкновение с ярким (NVG, вспышка, фонарь в морду, снег) — авто отступает
     к 1.0, а не выжигает всё; плюс knee приглушает клампинг.
  5. Тилт: gray-world по медианам каналов, сила ограничена — зелёнка Labs и
     синева ночи тянутся к нейтральному.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from dataclasses import dataclass, field


try:
    from . import correction
except ImportError:
    import correction

DEFAULT_CONFIG = {
    "enabled": True,
    "auto_exposure": True,
    "target_p25": 0.30,        # куда тянуть нижние середины тона
    "auto_strength": 1.00,     # 0.5 = деликатно, 1.4 = агрессивно
    "gamma_min": 0.95,         # пределы авто: «идеально» != «до бесконечности»
    "gamma_max": 2.20,
    "manual_gamma": 1.25,      # базовая гамма, когда авто выключено
    "gamma_bias": 1.00,        # ручной довесок поверх авто (F9 = 1.30)
    "shadow_lift": 0.30,       # подъём мёртвых чёрных (виньетка Таркова)
    "black_point": 0.80,       # сажать то, что ниже p05 (шум/виньетка) в ноль
    "contrast": 1.0,
    "auto_contrast": 0.30,     # контраст догоняет гамму: 1 + 0.3*(gamma-1) — без «мыла»
    "saturation": 1.12,
    "knee": 0.30,              # сажает пересветы, не трогаяmid-tones
    "clamp_floor": 2,          # 1..4 убирает «чёрные дыры» на IPS/OLED
    "anti_tint": True,
    "tint_strength": 0.35,     # 0 = не трогать цвета, 1 = полный gray-world
    "tint_max_gain": 1.15,
    "tint_min_median": 0.075,   # в полной темноте ББ не определить — не выдумываем
    "smooth": 0.30,            # EMA alpha за тик
    "deadband": 0.010,         # не трогаем таблицу, если сцена почти не изменилась
    "lift_min_factor": 0.25,   # во сколько раз минимуму гасим подъём чёрных на светлом
    "min_lut_delta": 3,        # уровней, при которых реально шлём ramp в GPU
    "update_hz": 12,
    "center_frac": 0.72,
    "capture_width": 560,
    "monitor_index": 1,          # mss-нумерация: 1 = основной, 2/3 = остальные
    "tie_to_game": True,       # только когда Тарков запущен (F8 включает в обход)
    "hotkeys": {"F8": "toggle", "F7": "restore", "F9": "boost", "F10": "profile"},
    "profile": "Tarkov — ночь / лес",
}

PROFILES = {
    "Tarkov — ночь / лес": {
        "target_p25": 0.30, "auto_strength": 1.10, "shadow_lift": 0.34,
        "black_point": 0.85, "auto_contrast": 0.34, "saturation": 1.16,
        "contrast": 1.0, "knee": 0.35, "tint_strength": 0.35, "tint_max_gain": 1.15,
    },
    "Tarkov — Reserve / Labs": {
        "target_p25": 0.34, "auto_strength": 0.70, "shadow_lift": 0.10,
        "black_point": 0.60, "auto_contrast": 0.18, "saturation": 1.06,
        "contrast": 1.0, "knee": 0.55, "tint_strength": 0.55, "tint_max_gain": 1.25,
    },
    "Tarkov — день / открытое": {
        "target_p25": 0.26, "auto_strength": 0.55, "shadow_lift": 0.06,
        "black_point": 0.50, "auto_contrast": 0.12, "saturation": 1.10,
        "contrast": 1.02, "knee": 0.25, "tint_strength": 0.30, "tint_max_gain": 1.12,
    },
    "Ручной (без авто)": {
        "auto_exposure": False, "manual_gamma": 1.35, "shadow_lift": 0.20,
        "black_point": 0.50, "auto_contrast": 0.0, "saturation": 1.10,
        "contrast": 1.08, "knee": 0.30, "tint_strength": 0.35,
    },
    "Максимум видимости (ради всего)": {
        "target_p25": 0.36, "auto_strength": 1.35, "gamma_max": 2.45,
        "shadow_lift": 0.30, "black_point": 0.95, "auto_contrast": 0.42,
        "saturation": 1.24, "contrast": 1.02, "knee": 0.50,
        "tint_strength": 0.45, "tint_max_gain": 1.20, "clamp_floor": 2,
    },
}


def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, "TarkovBright", "config.json")


def load_config(path: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = path or config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        _merge(cfg, user)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[TarkovBright] конфиг повреждён, беру настройки по умолчанию: {e}")
    return cfg


def save_config(cfg: dict, path: str | None = None) -> str:
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def _merge(dst: dict, src: dict):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        elif k in dst:
            dst[k] = v


# --------------------------------------------------------------------------
@dataclass
class EngineState:
    gamma: float = 1.0
    tint: list = field(default_factory=lambda: [1.0, 1.0, 1.0])
    last_median: float = -1.0
    last_luts: list = field(default_factory=list)
    ticks: int = 0
    applied: int = 0
    boost_until: float = 0.0


class BrightnessEngine:
    """Не знает ни про GUI, ни про Windows: принимает кадр, отдаёт LUT.
    `sink(blob_bytes)` — то, куда таблица уходит (на Windows — SetDeviceGammaRamp)."""

    def __init__(self, cfg: dict, sink=None):
        self.cfg = cfg
        self.sink = sink
        self.st = EngineState()
        self.info: dict = {}
        self._stop = threading.Event()
        self._grabber = None

    # -- вычисление ------------------------------------------------------
    def current_params(self, stats) -> dict:
        cfg = self.cfg
        boost = cfg["gamma_bias"]
        if self.st.boost_until > time.time():
            boost *= 1.30
        if cfg["auto_exposure"] and stats is not None:
            f = correction.auto_gamma_factor(
                stats, target_p25=cfg["target_p25"], strength=cfg["auto_strength"],
                gmin=cfg["gamma_min"], gmax=cfg["gamma_max"])
            f *= boost
            f = min(max(f, cfg["gamma_min"] * boost), cfg["gamma_max"] * boost)
        else:
            f = float(cfg["manual_gamma"]) * boost
        tint = [1.0, 1.0, 1.0]
        if (cfg["anti_tint"] and stats is not None
                and stats.median > cfg.get("tint_min_median", 0.075)):
            gains = correction.auto_tint(stats, max_gain=cfg["tint_max_gain"])
            s = cfg["tint_strength"]
            tint = [1.0 + (g - 1.0) * s for g in gains]
        return {"gamma": f, "tint": tint, "boost": boost}

    def effective_lift(self, stats) -> float:
        """Подъём чёрных уместен в темноте. В светлой сцене (Labs днём, снег)
        он лишь мылит картинку, поэтому плавно гаснет по мере роста p25."""
        lift = float(self.cfg["shadow_lift"])
        if lift <= 0 or stats is None:
            return lift
        k = 1.0 - (stats.p25 - 0.10) / 0.30
        return lift * min(1.0, max(self.cfg.get("lift_min_factor", 0.25), k))

    def build_luts(self, params: dict) -> list:
        cfg = self.cfg
        # контраст догоняет подъём: чем сильнее выкрутили гамму, тем больше
        # возвращаем «плотности», иначе тени превращаются в серое молоко
        contrast = float(cfg["contrast"]) * (1.0 + float(cfg["auto_contrast"]) * max(params["gamma"] - 1.0, 0.0))
        return correction.build_luts(
            gamma_factor=params["gamma"], tint=tuple(params["tint"]),
            shadow_lift=params["lift"], contrast=contrast,
            brightness=0.0, saturation=cfg["saturation"], knee=cfg["knee"],
            clamp_floor=cfg["clamp_floor"], black_point=params["black_point"])

    def _changed_enough(self, new_luts) -> bool:
        """Дёргать драйвер только если таблицы реально изменились: SetDeviceGammaRamp
        каждые 80 мс на некоторых драйверах даёт фликинг."""
        if not self.st.last_luts:
            return True
        return correction.lut_delta(self.st.last_luts, new_luts) >= self.cfg["min_lut_delta"]

    def step(self, frame) -> dict:
        """Один тик авто-подстройки. Возвращает инфо для статус-бара."""
        stats = correction.analyze(frame, self.cfg["center_frac"])
        self.st.ticks += 1

        target = self.current_params(stats)
        a = self.cfg["smooth"]

        moved = abs(stats.median - self.st.last_median) > self.cfg["deadband"]
        # при мёртвой зоне всё равно слегка подтягиваемся, чтобы не залипнуть
        alpha = a if moved else a * 0.25
        self.st.gamma += (target["gamma"] - self.st.gamma) * alpha
        for c in range(3):
            self.st.tint[c] += (target["tint"][c] - self.st.tint[c]) * alpha
        self.st.last_median = stats.median

        bp = 0.0
        if stats is not None and self.cfg["black_point"] > 0:
            # в линейном свете: всё, что ниже фактического «дна» сцены (p05),
            # уводим в ноль и нормируем остальное — это и есть анти-мыло
            bp = float(self.cfg["black_point"]) * min(max(stats.p05, 0.0) ** 2.2, 0.02)
        params = {"gamma": self.st.gamma, "tint": list(self.st.tint),
                  "boost": target["boost"], "lift": self.effective_lift(stats),
                  "black_point": bp}
        luts = self.build_luts(params)

        if self.cfg["enabled"] and self._changed_enough(luts):
            self.st.last_luts = luts
            self.st.applied += 1
            if self.sink is not None:
                self.sink(correction.ramp_bytes(luts))
        elif not self.cfg["enabled"]:
            self.st.last_luts = []

        self.info = {
            "stats": stats, "params": params, "luts": luts,
            "gamma": round(self.st.gamma, 3), "target": round(target["gamma"], 3),
            "tint": [round(t, 3) for t in self.st.tint], "moved": moved,
            "lift": round(params["lift"], 3), "black_point": round(bp, 5),
        }
        return self.info

    def restore_screen(self):
        self.st.last_luts = []
        self.st.gamma = 1.0
        self.st.tint = [1.0, 1.0, 1.0]
        if self.sink is not None:
            self.sink(None)          # None = «верни оригинал»

    def set_profile(self, name: str) -> str | None:
        if name not in PROFILES:
            return None
        self.cfg["profile"] = name
        self.cfg.update(copy.deepcopy(PROFILES[name]))
        self.st.last_luts = []
        return name

    def next_profile(self) -> str:
        names = list(PROFILES.keys())
        i = names.index(self.cfg.get("profile", names[0])) if self.cfg.get("profile") in names else -1
        return self.set_profile(names[(i + 1) % len(names)])

    def toggle_boost(self):
        self.st.boost_until = time.time() + 12.0

    # -- вспомогательное для тестов/скриптов -----------------------------
    def run_offline(self, frames, ticks_each: int = 1) -> dict:
        """Прогон пачки кадров без потока (используется tools/preview.py)."""
        info = {}
        for _ in range(max(1, ticks_each)):
            for f in frames:
                info = self.step(f)
        return info
