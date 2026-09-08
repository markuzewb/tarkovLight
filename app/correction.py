"""Математика авто-коррекции: экран -> статистика -> LUT для SetDeviceGammaRamp.

ЯДРО НА ЧИСТОМ PYTHON: numpy НЕ обязателен, чтобы программу можно было запустить
одной командой на любой машине с Python (pip не нужен вовсе). Если numpy установлен,
статистика кадра считается им — это быстрее и точнее (по всем пикселям, а не по сетке).

Чистый Python-путь и numpy-путь дают одинаковый результат в пределах 1 уровня —
за этим следит tests/test_nodeps.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

try:
    import numpy as _np
    HAVE_NUMPY = True
except Exception:                                      # noqa: BLE001
    _np = None
    HAVE_NUMPY = False

RAMP_SIZE = 256
SHADOW_LIFT_MAX = 0.02   # ползунок 1.0 => +5 уровней к абсолютному чёрному
HIGHLIGHT_KEEP = 0.985   # до какого уровня в sRGB разрешаем поднять света
TINT_TRIM_EXP = 1.30     # см. _build_channel_lut: трип степенью мягче гейна, компенсируем


# --------------------------------------------------------------------------
# Статистика кадра
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FrameStats:
    """Агрегаты одного кадра. Значения нормализованы в 0..1."""
    p05: float          # почти-чёрные (детали в тенях)
    p25: float          # нижние середины тона — главная точка авто-гаммы
    median: float       # медиана яркости
    p95: float          # светлые участки
    mean: float
    clip_hi: float      # доля пикселей >= 250 (пересвет/NVG-блум)
    clip_lo: float      # доля пикселей <= 5  (мёртвый чёрный, виньетка)
    chan_median: tuple  # медианы R, G, B
    mean_rgb: tuple     # средние R, G, B (gray-world)
    band_rgb: tuple = ()   # средние R,G,B в полосе p55..p92 — по ним тилт честнее


def _percentile_list(hist, total, q):
    """Квантиль по 256-уровневой гистограмме (int levels 0..255 -> 0..1)."""
    if total <= 0:
        return 0.0
    target = q * total
    acc = 0
    for i, c in enumerate(hist):
        acc += c
        if acc >= target:
            return i / 255.0
    return 1.0


def _stats_from_sample(pix, gh, chs, n):
    """Собирает FrameStats из гистограмм (общо для numpy- и pure-путей)."""
    mean_g = sum(i * gh[i] for i in range(256)) / 255.0 / max(n, 1)
    mean_rgb = tuple(sum(i * chs[c][i] for i in range(256)) / 255.0 / max(n, 1) for c in range(3))
    lo = int(_percentile_list(gh, n, 0.55) * 255)
    hi = int(_percentile_list(gh, n, 0.92) * 255)
    band_rgb = ()
    if pix is not None:
        bsum = [0, 0, 0]
        bn = 0
        for r, g, b in pix:
            v = (r * 54 + g * 183 + b * 19) >> 8
            if lo <= v <= hi:
                bsum[0] += r
                bsum[1] += g
                bsum[2] += b
                bn += 1
        if bn > max(8, (len(pix)) * 0.03):
            band_rgb = tuple(v / bn / 255.0 for v in bsum)
    return FrameStats(
        p05=_percentile_list(gh, n, 0.05),
        p25=_percentile_list(gh, n, 0.25),
        median=_percentile_list(gh, n, 0.50),
        p95=_percentile_list(gh, n, 0.95),
        mean=mean_g,
        clip_hi=sum(gh[250:]) / max(n, 1),
        clip_lo=sum(gh[:6]) / max(n, 1),
        chan_median=tuple(_percentile_list(chs[c], n, 0.5) for c in range(3)),
        mean_rgb=mean_rgb,
        band_rgb=band_rgb,
    )


def analyze(src, center_frac: float = 0.7) -> FrameStats:
    """src: либо capture.Frame (без зависимостей), либо ndarray (H, W, 3) uint8 RGB.

    center_frac < 1 режет края кадра: в Таркове тяжёлая виньетка и тёмные
    полосы по периметру — по всему кадру статистика врёт, и авто-гамма
    уезжает в максимум на пустом месте.
    """
    if HAVE_NUMPY and isinstance(src, _np.ndarray):
        return _analyze_numpy(src, center_frac)
    if HAVE_NUMPY:
        try:
            return _analyze_numpy(src.as_numpy(), center_frac)
        except Exception:                              # noqa: BLE001
            pass
    return _analyze_frame(src, center_frac)


def _analyze_frame(fr, center_frac) -> "FrameStats":
    """Pure-Python путь (numpy не нужен).

    Берём несколько полных горизонтальных полос кадра (строки распределены по
    высоте РАВНОМЕРНО), внутри каждой — 2x2 box. Два момента, которые дорого
    стоят в качестве:

    * Полосы вместо квадратной сетки: сетка резонирует с периодической
      структурой кадра (окна, светящиеся пиксели) и уходила по квантилям на
      6+ уровней.
    * Индексы строк считаем делением диапазона, а не шагом `y += step`:
      шаг с округлением вниз систематически выкидывал низ кадра (до 9%),
      что смещало p25 и авто-гамму сильнее, чем шум самой выборки.
    """
    w, h, stride, data = fr.width, fr.height, fr.stride, fr.pixels
    bpp, (ro, go, bo) = fr.bpp, fr.order
    ch = max(16, int(h * center_frac))
    cw = max(16, int(w * center_frac))
    y0 = (h - ch) // 2
    x0 = (w - cw) // 2
    y1 = min(h, y0 + ch)
    x1 = min(w, x0 + cw)
    x_end = x1 - 1 if x1 - x0 >= 2 else x1          # box 2x2: нужен следующий пиксель/строка
    y_end = y1 - 1 if y1 - y0 >= 2 else y1
    per_row = max(1, (x_end - x0) // 2)
    # ~6000 боксов: меньше — квантили уже «гуляют» на 20+ уровней на шумных
    # сценах, больше — заметная загрузка ядра (без numpy это ~4 мс на кадр)
    rows = max(2, min(max(1, y_end - y0), int(6000 / per_row) + 1))

    gh = [0] * 256
    hR, hG, hB = [0] * 256, [0] * 256, [0] * 256
    pix = []
    n = 0
    span = max(1, y_end - y0 - 1)
    for i in range(rows):
        y = y0 + (i * span) // rows
        rb = y * stride
        rb2 = rb + stride
        for x in range(x0, x_end, 2):
            o = rb + x * bpp
            o2 = o + bpp
            o3 = rb2 + x * bpp
            o4 = o3 + bpp
            r = (data[o + ro] + data[o2 + ro] + data[o3 + ro] + data[o4 + ro]) >> 2
            g = (data[o + go] + data[o2 + go] + data[o3 + go] + data[o4 + go]) >> 2
            b = (data[o + bo] + data[o2 + bo] + data[o3 + bo] + data[o4 + bo]) >> 2
            v = (r * 54 + g * 183 + b * 19) >> 8
            gh[v] += 1
            hR[r] += 1
            hG[g] += 1
            hB[b] += 1
            pix.append((r, g, b))
            n += 1
    return _stats_from_sample(pix, gh, (hR, hG, hB), n)


def _analyze_numpy(frame, center_frac) -> "FrameStats":
    """Быстрый путь: все пиксели, целочисленные гистограммы (~0.7 мс на 560x315)."""
    np = _np
    h, w = frame.shape[:2]
    if center_frac < 1.0:
        ch = max(16, int(h * center_frac))
        cw = max(16, int(w * center_frac))
        y0 = (h - ch) // 2
        x0 = (w - cw) // 2
        frame = frame[y0:y0 + ch, x0:x0 + cw]

    fh, fw = frame.shape[:2]
    if fh * fw > 400_000:                              # огромный кадр -> every-2nd (view)
        frame = frame[::2, ::2]
        fh, fw = frame.shape[:2]
    fh -= fh % 2
    fw -= fw % 2
    frame = frame[:fh, :fw]
    if fh >= 4 and fw >= 4:
        f = frame[0::2, 0::2].astype(np.uint16)        # 2x2 box: сумма <= 1020 -> >>2
        f += frame[1::2, 0::2]
        f += frame[0::2, 1::2]
        f += frame[1::2, 1::2]
        f >>= 2
    else:
        f = frame.astype(np.uint16)

    gray = ((f[..., 0] * 54 + f[..., 1] * 183 + f[..., 2] * 19) >> 8).astype(np.uint8)
    n = int(gray.size)
    ghist = np.bincount(gray.ravel(), minlength=256)
    hists = [np.bincount(f[..., c].astype(np.uint8).ravel(), minlength=256) for c in range(3)]
    idx = np.arange(256, dtype=np.float64)

    lo = int(_percentile_list([int(v) for v in ghist], n, 0.55) * 255)
    hi = int(_percentile_list([int(v) for v in ghist], n, 0.92) * 255)
    band = (gray >= lo) & (gray <= hi)
    if int(band.sum()) > n * 0.03:
        band_rgb = tuple(float(b[band].mean()) / 255.0 for b in (f[..., 0], f[..., 1], f[..., 2]))
    else:
        band_rgb = ()

    return FrameStats(
        p05=_percentile_np(ghist, n, 0.05),
        p25=_percentile_np(ghist, n, 0.25),
        median=_percentile_np(ghist, n, 0.50),
        p95=_percentile_np(ghist, n, 0.95),
        mean=float(ghist @ idx / 255.0 / max(n, 1)),
        clip_hi=float(ghist[250:].sum() / max(n, 1)),
        clip_lo=float(ghist[:6].sum() / max(n, 1)),
        chan_median=tuple(_percentile_np(hh, n, 0.5) for hh in hists),
        mean_rgb=tuple(float(hh @ idx / 255.0 / max(n, 1)) for hh in hists),
        band_rgb=band_rgb,
    )


def _percentile_np(hist, total, q):
    if total <= 0:
        return 0.0
    idx = int(_np.searchsorted(_np.cumsum(hist), q * total))
    return min(max(idx, 0), 255) / 255.0


# --------------------------------------------------------------------------
# Авто-решение: статистика -> параметры
# --------------------------------------------------------------------------
def auto_gamma_factor(stats: FrameStats, target_p25: float = 0.30,
                      strength: float = 1.0, gmin: float = 0.80,
                      gmax: float = 2.60) -> float:
    """Во сколько раз поднимем середины тона.

    gamma_factor = (target / p25) ^ strength: плавное усиление, а не «в лоб».
    В полной темноте f -> gmax, в светлой сцене f -> 1, на снежку/блуме f < 1.
    """
    p25 = max(stats.p25, 1.0 / 255.0)
    if stats.p95 >= 0.98 and stats.clip_hi > 0.06:
        return 1.0                                     # кадр залит светом — не выкручиваем
    f = (max(target_p25, p25) / p25) ** float(strength)
    if stats.p95 > 0.80:                               # потолок по светам: p95 не прилипаем к 255
        hi = math.log(stats.p95) / math.log(HIGHLIGHT_KEEP)
        if hi > 1e-3:
            f = min(f, hi)
    return float(min(max(f, gmin), gmax))


def auto_tint(stats: FrameStats, max_gain: float = 1.35) -> tuple:
    """Gray-world баланс белого по полосе полутонов/светов (против сине-зелёного
    тилта Таркова). Гейны нормированы так, что геометрическое среднее = 1,
    т.е. общая яркость не уезжает."""
    src = stats.band_rgb if stats.band_rgb else stats.mean_rgb
    means = [max(float(m), 1.0 / 255.0) for m in src]
    avg = sum(means) / 3.0
    gains = [min(max(avg / m, 1.0 / max_gain), max_gain) for m in means]
    geo = (gains[0] * gains[1] * gains[2]) ** (1.0 / 3.0)
    gains = [min(max(g / geo, 1.0 / max_gain), max_gain) for g in gains]
    return tuple(gains)


# --------------------------------------------------------------------------
# LUT (256 записей на канал, уровни 0..255)
# --------------------------------------------------------------------------
def build_luts(gamma_factor=1.0, tint=(1.0, 1.0, 1.0), shadow_lift=0.0,
               contrast=1.0, brightness=0.0, saturation=1.0, knee=0.0,
               clamp_floor=0.0, black_point=0.0):
    """Три таблицы по 256 значений (уровни 0..255) для R, G, B.

    Вся градация считается в float по всем трём каналам сразу и лишь потом
    округляется: насыщение вокруг luminance, посчитанное уже по округлённым
    уровням, давало ненарастающие LUT (1-уровневые «запинки» -> бандинг).

    В устройство таблицы отдаёт только ramp_bytes() (там масштаб 0..65535).
    """
    lift = SHADOW_LIFT_MAX * shadow_lift
    tint = tuple(float(t) for t in tint)
    inv_f = [1.0 / (max(gamma_factor, 1e-3) * max(t, 1e-3) ** TINT_TRIM_EXP) for t in tint]
    sat = float(saturation)
    do_sat = abs(sat - 1.0) > 1e-4
    out = ([0] * RAMP_SIZE, [0] * RAMP_SIZE, [0] * RAMP_SIZE)

    for i in range(RAMP_SIZE):
        srgb = i / 255.0
        vals = [0.0, 0.0, 0.0]
        for c in range(3):
            # 1) чёрная точка: что ниже «дна» кадра (виньетка/шум) — в ноль +
            #    нормировка. Именно это возвращает плотность теням после
            #    подъёма гаммы, а не превращает кадр в серое молоко.
            lin = srgb ** 2.2
            if black_point > 1e-6:
                lin = min(max((lin - black_point) / (1.0 - black_point), 0.0), 1.0)
            x = lin ** (1.0 / 2.2)

            # 2) подъём чёрных — в encoded-домене (как «Black level» монитора):
            #    в линейном свете те же 2% дали бы полное молочноe дно.
            if lift > 0.0:
                x = min(x + lift * (1.0 - x) ** 2, 1.0)

            # 3) гамма + тилт ровно как на мониторе: out = in ^ (1/f).
            #    f = 1.0 и tint = 1.0 -> ТОЖДЕСТВЕННАЯ таблица;
            #    tint > 1 = «каналу нужно больше света» -> степень меньше.
            x = min(max(x, 0.0), 1.0) ** inv_f[c]

            # 4) ручные регулировки
            if contrast != 1.0 or brightness != 0.0:
                x = (x - 0.5) * contrast + 0.5 + brightness
            if knee > 1e-3:
                # filmic-скат: глушим клампинг пересветов (NVG/вспышка/снег);
                # множитель (1+knee) оставляет белый белым.
                xc = min(max(x, 0.0), 1.0)
                x = xc * (1.0 + knee) / (1.0 + knee * xc)
            vals[c] = min(max(x, 0.0), 1.0)

        if do_sat:
            # 5) насыщенность вокруг яркости в линейном свете:
            #    x' = lum + (x - lum) * s. lum из НЕокруглённых значений.
            lin = [v ** 2.2 for v in vals]
            lum = lin[0] * 0.2126 + lin[1] * 0.7152 + lin[2] * 0.0722
            for c in range(3):
                vals[c] = min(max(lum + (lin[c] - lum) * sat, 0.0), 1.0) ** (1.0 / 2.2)

        for c in range(3):
            v = vals[c] * 255.0
            if clamp_floor > 0:
                v = max(v, clamp_floor)          # чёрный уровня OLED/IPS glow
            out[c][i] = int(round(v))
    return out


def ramp_bytes(luts):
    """Упаковка для SetDeviceGammaRamp: 1536 байт = 3 x 256 WORD, little-endian.

    РАСКЛАДА КРИТИЧНА: Windows ожидает C-шный массив `WORD Ramp[3][256]`,
    то есть ПЛАНАМИ — сначала все 256 значений красного, затем зелёного, затем
    синего. Перемешанные triplets (r0,g0,b0,...) на identity-таблице выглядят
    точно так же, поэтому проверить их «на глаз» невозможно, но драйвер при
    не-identity раскладке видит немонотонный массив и отвечает
    ERROR_INVALID_PARAMETER (отказ SetDeviceGammaRamp).

    Значения в таблице — 0..65535, а не 0..255; для целых уровней это ровно
    *257, поэтому 1:1 таблица получается без ступенек.
    """
    vals = []
    for c in range(3):                       # планарно: R[256], G[256], B[256]
        lut = luts[c]
        for i in range(RAMP_SIZE):
            v = lut[i] * 257
            vals.append(65535 if v > 65535 else (0 if v < 0 else v))
    blob = _pack(vals)
    if len(blob) != 1536:
        raise AssertionError("размер ramp должен быть 1536 байт, получил %d" % len(blob))
    return blob


def _pack(vals):
    try:
        import struct
        return struct.pack("<%dH" % len(vals), *vals)
    except Exception:                                  # noqa: BLE001
        out = bytearray()
        for v in vals:
            out += bytes((v & 0xFF, (v >> 8) & 0xFF))
        return bytes(out)


def _ramp_words(vals, per_channel=RAMP_SIZE):
    """Проверка ровно того, что требует драйвер: 3 плана по per_channel WORD,
    значения 0..65535, каждый блок не убывает. '' = ок, иначе — текст проблемы."""
    if len(vals) != per_channel * 3:
        return "длина таблицы %d, ожидалось %d" % (len(vals), per_channel * 3)
    for c in range(3):
        base = c * per_channel
        prev = -1
        for i in range(per_channel):
            v = vals[base + i]
            if v < prev:
                return "канал %d: значение падает на уровне %d (%d -> %d)" % (c, i, prev, v)
            if not (0 <= v <= 65535):
                return "канал %d: значение %d вне 0..65535" % (c, v)
            prev = v
    return ""


def identity_ramp() -> bytes:
    """Ровно то, что стоит в Windows «по умолчанию» (i*257, планарно)."""
    lin = [i * 257 for i in range(RAMP_SIZE)]
    return _pack(lin + lin + lin)


def lut_delta(a, b):
    """Максимальная разница уровней между двумя наборами таблиц (для порога
    «стоит ли вообще дёргать драйвер»)."""
    if not a or not b:
        return 999
    d = 0
    for ca, cb in zip(a, b):
        for x, y in zip(ca, cb):
            v = x - y
            if v < 0:
                v = -v
            if v > d:
                d = v
    return d


# --------------------------------------------------------------------------
# Опциональные numpy-хелперы (нужны только инструментам превью/тестов)
# --------------------------------------------------------------------------
def apply_luts(frame, luts):
    """LUT-коррекция картинки ndarray (H, W, 3) uint8 — ровно та математика,
    что уходит в gamma-таблицу. Требует numpy (он есть у tools/)."""
    if not HAVE_NUMPY:
        raise RuntimeError("apply_luts требует numpy (он нужен только для превью по файлам)")
    np = _np
    out = np.empty_like(frame, dtype=np.uint8)
    idx = frame.astype(np.uint16)
    for c in range(3):
        out[:, :, c] = np.asarray(luts[c], dtype=np.uint16)[idx[:, :, c]]
    return out
