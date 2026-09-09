"""Захват кадра экрана. Порядок бэкендов:

  1. GDI (ctypes, StretchBlt с HALFTONE)  — не требует НИ ОДНОЙ сторонней библиотеки
  2. mss                                 — если стоит (тот же GDI, чуть удобнее)
  3. Pillow                              — если стоит только он
  4. none                                — тогда приложение работает в ручном режиме

Все бэкенды отдают capture.Frame — упакованные байты + параметры строки. Так один и
тот же код работает и с numpy, и без него (см. correction.analyze).

ВАЖНО: GDI/mss не видят содержимое Exclusive Fullscreen (вернут чёрный кадр).
Для Таркова это не проблема в Borderless/окне; прога сама предупредит, если
кадры чёрные.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform.startswith("win")

_MAX_W_DEFAULT = 560
# Как часто перечитывать список мониторов у mss. Раньше _grab_mss дёргал
# sct.monitors НА КАЖДЫЙ кадр — то есть EnumDisplayMonitors через COM на 12 Гц.
# Теперь держим кэш и обновляем его не чаще раза в 2 с: этого хватает, чтобы
# поймать «горячее» подключение/отключение монитора, а лишний расход уходит.
_MON_REFRESH_S = 2.0
_HALFTONE = 4
_SRCCOPY = 0x00CC0020
_DIB_RGB_COLORS = 0
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1


class Frame:
    """Один кадр: packed-байты + геометрия. order=(0,1,2) для RGB, (2,1,0) для BGR."""

    __slots__ = ("pixels", "width", "height", "stride", "bpp", "order")

    def __init__(self, pixels, width, height, stride=None, bpp=3, order=(0, 1, 2)):
        self.pixels = pixels
        self.width = width
        self.height = height
        self.stride = stride or ((width * bpp + 3) // 4) * 4
        self.bpp = bpp
        self.order = order

    @property
    def size(self):
        return self.width * self.height

    def as_numpy(self):
        import numpy as np
        row, w, h, bpp = self.stride, self.width, self.height, self.bpp
        base = np.frombuffer(self.pixels, dtype=np.uint8)[: h * row]
        mat = base.reshape(h, row)[:, : w * bpp].reshape(h, w, bpp)
        return np.ascontiguousarray(mat[:, :, list(self.order)])


class Grabber:
    """Создавать в том потоке, который дёргает grab() (GDI-DC привязан к потоку)."""

    def __init__(self, max_width: int = _MAX_W_DEFAULT, monitor: int = 1):
        self.max_width = max(64, int(max_width))
        self.monitor = max(1, int(monitor))   # mss: 1 = основной, 2.. = остальные
        self.backend = "none"
        self.last_error = ""
        self._sct = None
        self._pil = None
        self._mon_cache = None      # кэш списка мониторов mss (см. _grab_mss)
        self._mon_ts = 0.0          # когда список читали в последний раз (time.monotonic)
        self._init_backend()

    # -- выбор бэкенда --------------------------------------------------
    def _init_backend(self):
        if IS_WINDOWS:
            try:
                # _gdi_ready() лишь проверяет, что ctypes-ручки поднялись. Для
                # «бэкенд захвата: gdi» этого мало: v1.3.1 на отключённом сеансе
                # рапортовал gdi, а grab() падал — и врач врал про «нет бэкенда».
                # Поэтому выбираем бэкенд пробным кадром.
                if _gdi_ready():
                    _grab_gdi(64)
                    self.backend = "gdi"
                    return
            except Exception as e:                      # noqa: BLE001
                self.last_error = f"gdi: {e}"
        try:
            import mss  # type: ignore
            self._sct = mss.mss()
            self.backend = "mss"
            return
        except Exception as e:                          # noqa: BLE001
            self.last_error = self.last_error or f"mss: {e}"
        try:
            from PIL import ImageGrab
            self._pil = ImageGrab
            self.backend = "pillow"
            return
        except Exception as e:                          # noqa: BLE001
            self.last_error = self.last_error or f"pillow: {e}"

    @property
    def available(self) -> bool:
        return self.backend != "none"

    # -- захват ---------------------------------------------------------
    def grab(self):
        """-> Frame | None."""
        try:
            if self.backend == "gdi":
                return _grab_gdi(self.max_width)
            if self.backend == "mss":
                return _grab_mss(self)
            if self.backend == "pillow":
                return _grab_pillow(self)
        except Exception as e:                          # noqa: BLE001
            self.last_error = str(e)
        return None

    def close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:                           # noqa: BLE001
                pass


def _grab_mss(g: Grabber):
    # Обращение к sct.monitors дорогое (EnumDisplayMonitors через COM), поэтому
    # не читаем его на каждый кадр: держим кэш и обновляем не чаще раза в 2 с —
    # этого достаточно, чтобы подхватить «горячее» подключение/отключение
    # монитора, а в статике на 12 Гц ОС дёргается ~раз в 2 с вместо 12 раз/с.
    now = time.monotonic()
    if g._mon_cache is None or (now - g._mon_ts) > _MON_REFRESH_S:
        try:
            g._mon_cache = g._sct.monitors
            g._mon_ts = now
        except Exception as e:                       # noqa: BLE001
            g.last_error = f"mss.monitors: {e}"
            if g._mon_cache is None:
                return None
    mon_cache = g._mon_cache
    idx = g.monitor if g.monitor < len(mon_cache) else 1
    if idx >= len(mon_cache):
        g.last_error = f"нет монитора #{g.monitor}"
        return None
    mon = mon_cache[idx]
    shot = g._sct.grab(mon)
    return Frame(shot.bgra, shot.width, shot.height,
                 stride=shot.width * 4, bpp=4, order=(2, 1, 0))


def _grab_pillow(g: Grabber):
    img = g._pil.grab(all_screens=False).convert("RGB")
    w, h = img.size
    if w > g.max_width:
        from PIL import Image
        img = img.resize((g.max_width, max(16, int(h * g.max_width / w))), Image.BOX)
        w, h = img.size
    return Frame(img.tobytes("raw", "RGB"), w, h, stride=w * 3, bpp=3, order=(0, 1, 2))


# --------------------------------------------------------------------------
# GDI: StretchBlt всего экрана в маленький 24-битный DIB. Без зависимостей.
# --------------------------------------------------------------------------
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_gdi = None


def _gdi_ready() -> bool:
    global _gdi
    if not IS_WINDOWS:
        return False
    if _gdi is None:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.CreateDIBSection.restype = ctypes.c_void_p
        gdi32.SelectObject.restype = ctypes.c_void_p
        user32.GetDC.restype = ctypes.c_void_p
        _gdi = (user32, gdi32)
    return _gdi is not None


def _grab_gdi(max_width: int) -> Frame:
    user32, gdi32 = _gdi
    src = user32.GetDC(0)
    if not src:
        raise RuntimeError("GetDC(0) вернул 0")
    try:
        sw = user32.GetSystemMetrics(_SM_CXSCREEN)
        sh = user32.GetSystemMetrics(_SM_CYSCREEN)
        if sw < 16 or sh < 16:
            raise RuntimeError(f"экран {sw}x{sh} — странно")
        w = min(max_width, sw)
        h = max(16, int(sh * w / sw))
        stride = ((w * 3 + 3) // 4) * 4

        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(bmi)
        bmi.biWidth = w
        bmi.biHeight = -h                 # top-down: строка 0 = верх экрана
        bmi.biPlanes = 1
        bmi.biBitCount = 24
        bmi.biCompression = 0

        memdc = gdi32.CreateCompatibleDC(src)
        bits_ptr = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(memdc, ctypes.byref(bmi), _DIB_RGB_COLORS,
                                     ctypes.byref(bits_ptr), None, 0)
        if not bmp:
            raise RuntimeError("CreateDIBSection не удался")
        old = gdi32.SelectObject(memdc, bmp)
        try:
            gdi32.SetStretchBltMode(memdc, _HALFTONE)      # area-усреднение при даунскейле
            ok = gdi32.StretchBlt(memdc, 0, 0, w, h, src, 0, 0, sw, sh, _SRCCOPY)
            if not ok:
                raise RuntimeError("StretchBlt вернул 0")
            data = ctypes.string_at(bits_ptr, stride * h)
        finally:
            gdi32.SelectObject(memdc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(memdc)
    finally:
        user32.ReleaseDC(0, src)
    return Frame(data, w, h, stride=stride, bpp=3, order=(2, 1, 0))   # DIB = BGR
