"""Всё, что касается Windows: gamma-таблица устройства, хоткеи, определение
игры, HDR-проба, яркость монитора. Только ctypes, без pywin32.

Если импорт не удаётся (запуск на Linux/macOS), модуль помечается
IS_WINDOWS = False и приложение работает в режиме превью/расчёта.
"""
from __future__ import annotations

import ctypes
import functools
import struct
import os
import subprocess
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform.startswith("win")

try:                        # работаем и как пакет (app/), и как голый скрипт
    from . import correction
except ImportError:
    import correction

GAMMA_MIN = 128
GAMMA_MAX = 2048
RAMP_BYTES = 256 * 3 * 2          # WORD[3][256]

VK_F7, VK_F8, VK_F9, VK_F10 = 0x76, 0x77, 0x78, 0x79

_GAME_PROCESSES = (
    "escapefromtarkov.exe",
    "sunityservice.exe",          # стартер ЕНК — считаем «игрой»
)

# коды Win32, которые реально возвращает SetDeviceGammaRamp
_WINERR = {
    1: "ERROR_INVALID_FUNCTION — драйвер не поддерживает гамма-таблицу "
       "(частое дело на базовом/виртуальном драйвере и в RDP)",
    6: "ERROR_INVALID_HANDLE — не удалось получить DC экрана",
    87: "ERROR_INVALID_PARAMETER — таблицу отклонили: 3 блока по 256 WORD, "
        "значения 0..65535 и каждый блок обязан не убывать",
    120: "ERROR_CALL_NOT_IMPLEMENTED — функция недоступна в этом сеансе "
         "(удалённый рабочий стол / some remote-.session drivers)",
    1468: "ERROR_NOT_SUPPORTED — драйвер явно не даёт программную гамму",
}

if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _kernel32 = ctypes.windll.kernel32
    _kernel32.GetLastError.restype = ctypes.c_ulong

    _user32.GetDC.restype = wintypes.HDC
    _gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
    _gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL
    _gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
    _gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL


@functools.lru_cache(maxsize=1)
def _display_names() -> tuple:
    r"""Активные имена устройств вывода: \\.\DISPLAY1, \\.\DISPLAY2, ..."""
    names = []
    try:
        class _DEV(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                        ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                        ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]
        i = 0
        while i < 8:
            d = _DEV()
            d.cb = ctypes.sizeof(d)
            if not _user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
                break
            if d.StateFlags & 1:                      # DISPLAY_DEVICE_ACTIVE
                names.append(str(d.DeviceName))
            i += 1
    except Exception:
        pass
    return tuple(names)


class _Dc:
    """Лестница попыток получить DC: виртуальный экран -> DC конкретного монитора.

    Нужно потому, что часть стеков (гибридная графика ноутбуков, KVM, несколько
    адаптеров) возвращает ERROR_INVALID_HANDLE для GetDC(NULL), но принимает DC
    устройства. Перебираем их при отказе, а не при каждом вызове.
    """

    def __init__(self):
        self.hdc = 0
        self.source = "не создавался"
        self._dev = -1

    def get(self):
        if not self.hdc:
            self.next()
        return self.hdc

    def next(self) -> bool:
        """Следующий доступный DC; True, если удалось переключиться."""
        if self.hdc:
            try:
                _user32.ReleaseDC(0, self.hdc)
            except Exception:
                pass
            self.hdc = 0
        if self._dev < 0:
            self._dev = 0
            self.source = "GetDC(весь экран)"
            try:
                hdc = _user32.GetDC(0)
            except Exception:
                hdc = 0
            if hdc:
                self.hdc = hdc
                return True
            return False
        names = _display_names()
        while self._dev < len(names):
            name = names[self._dev]
            self._dev += 1
            try:
                hdc = _gdi32.CreateDCW("DISPLAY", name, None, None)
            except Exception:
                hdc = 0
            if hdc:
                self.hdc = hdc
                self.source = "CreateDC(%s)" % name
                return True
        return False


def _ramp_words_local(vals, per_channel: int = 256) -> str:
    """Одна реализация на оба размера — см. correction._ramp_words (тот же код,
    что и требования драйвера: 3 блока, 0..65535, монотонно неубывающе)."""
    return correction._ramp_words(vals, per_channel)


def _upsample_1024(words256) -> list:
    """3x256 -> 3x1024 линейной интерполяцией (некоторые драйверы и 10-битные
    режимы принимают только расширенную таблицу)."""
    out = []
    for c in range(3):
        base = c * 256
        for i in range(1024):
            pos = i * 255 / 1023.0
            lo = int(pos)
            hi = min(lo + 1, 255)
            frac = pos - lo
            out.append(int(round(words256[base + lo] * (1.0 - frac) + words256[base + hi] * frac)))
    return out


def _downsample_256(vals1024) -> list:
    """3x1024 -> 3x256: берём каждую 4-ю запись (для «заводской» таблицы это точно)."""
    out = []
    for c in range(3):
        base = c * 1024
        for i in range(256):
            out.append(int(vals1024[base + min(i * 4, 1023)]))
    return out


class GammaRamp:
    """Владеет системной gamma-таблицей: ставим свою, при выходе — железно
    восстанавливаем оригинал (в т.ч. по atexit / Ctrl+C)."""

    def __init__(self, hdc=None):
        # hdc=None -> ленивое получение DC внутри _Dc (виртуальный экран, при
        # отказе — CreateDC конкретного устройства). Не забыть бы это: с
        # self._dc = None свойство hdc вернуло бы 0 и драйвер ответил бы отказом.
        self._dc = _FixedDc(hdc) if hdc is not None else _Dc()
        self._saved = None
        self._active = False
        self.last_error = 0                 # GetLastError после отказа
        self.last_note = ""                 # локальная причина (до вызова API)
        self.ramp_mode = 256                # 256 = WORD Ramp[3][256], 1024 = расширенная

    # -- расшифровка отказа --------------------------------------------
    def error_text(self) -> str:
        parts = []
        if self.last_note:
            parts.append(self.last_note)
        if self.last_error:
            parts.append("код Windows %d: %s" % (self.last_error,
                                                 _WINERR.get(self.last_error, "неизвестная ошибка")))
        if parts:
            return "; ".join(parts)
        return ("драйвер вернул FALSE без кода ошибки — так обычно выглядит отказ в "
                "удалённом сеансе (RDP) или на базовом драйвере дисплея")

    @property
    def source(self) -> str:
        return self._dc.source if self._dc else "не создавался"

    # -- низкоуровневые -------------------------------------------------
    @property
    def hdc(self):
        return self._dc.get() if self._dc else 0

    def reset_dc(self):
        """Пересоздать DC (после смены режима экрана/монитора)."""
        self._dc = _Dc()

    def _read(self) -> bytes | None:
        """Текущая таблица, всегда в нормализованном виде 3x256 (1536 байт)."""
        if not IS_WINDOWS:
            return None
        for n in (self.ramp_mode, 1024 if self.ramp_mode == 256 else 256):
            if not n:
                continue
            try:
                buf = (ctypes.c_ushort * (n * 3))()
                if _gdi32.GetDeviceGammaRamp(self.hdc, ctypes.byref(buf)):
                    vals = list(buf)
                    if n == 1024:
                        vals = _downsample_256(vals)
                    return struct.pack("<768H", *vals)
            except Exception:
                continue
        return None

    def _write(self, blob: bytes) -> bool:
        if not IS_WINDOWS:
            return False
        if len(blob) != RAMP_BYTES:
            raise ValueError(f"размер ramp = {len(blob)}, ожидалось {RAMP_BYTES}")
        words = [int(v) for v in struct.unpack("<768H", blob)]
        problem = _ramp_words_local(words, 256)
        if problem:
            self.last_note = "таблица не прошла локальную проверку: " + problem
            self.last_error = 0
            return False
        self.last_note = ""
        attempts = [256, 1024] if self.ramp_mode == 256 else [1024, 256]
        for n in attempts:
            data = words if n == 256 else _upsample_1024(words)
            if n == 1024 and _ramp_words_local(data, 1024):
                continue
            ok = False
            for _ in range(2):                     # вторая попытка — DC другого устройства
                buf = (ctypes.c_ushort * len(data))(*data)
                try:
                    ok = bool(_gdi32.SetDeviceGammaRamp(self.hdc, ctypes.byref(buf)))
                except Exception as e:
                    self.last_note = f"исключение при вызове SetDeviceGammaRamp: {e}"
                    return False
                if ok:
                    break
                self.last_error = int(_kernel32.GetLastError())
                if not (self.hdc == 0 or self.last_error in (6, 1)) or not self._dc or not self._dc.next():
                    break
            if ok:
                self.ramp_mode = n
                self.last_error = 0
                return True
        return False

    # -- публичное API --------------------------------------------------
    def save_original(self) -> None:
        cur = self._read()
        if cur is not None and self._saved is None:
            self._saved = bytes(cur)

    @property
    def saved_original(self) -> bytes | None:
        return self._saved

    def apply(self, blob: bytes) -> bool:
        if not self._active:
            self.save_original()
            self._active = True
        return self._write(blob)

    def force_identity(self) -> bool:
        """Жёстко ставит заводскую таблицу 1:1 — аварийный ключ --restore
        (на случай, если процесс убили и гамма осталась выкрученной)."""
        return self._write(_identity_blob())

    def restore(self) -> None:
        if not self._active:
            return
        ok = self._write(self._saved) if self._saved else self._write(_identity_blob())
        if not ok:
            ok = self._write(_identity_blob())      # оригинал не прочитали — хотя бы 1:1
        self._active = False
        return ok

    @property
    def active(self) -> bool:
        return self._active


class _FixedDc:
    """Обёртка для переданного извне DC (тесты), чтобы не ветвить код выше."""

    def __init__(self, hdc):
        self.hdc = hdc
        self.source = "передан извне"

    def get(self):
        return self.hdc

    def next(self) -> bool:
        return False


def _buf_to_bytes(buf) -> bytes:
    return bytes(memoryview(buf).tobytes())


def _identity_blob() -> bytes:
    """Заводская 1:1 таблица. Один источник правды — correction.identity_ramp()
    (раскладка планарная, см. ramp_bytes)."""
    return correction.identity_ramp()


def gamma_env_report() -> dict:
    """Почему таблица может не ставиться — то, что видно из Windows без догадок."""
    rep = {}
    if not IS_WINDOWS:
        return {"note": "не Windows"}
    try:
        rep["remote_session"] = bool(_user32.GetSystemMetrics(78))      # SM_REMOTESESSION
    except Exception:
        pass
    hdc = 0
    try:
        hdc = _user32.GetDC(0)
        bits = int(_gdi32.GetDeviceCaps(hdc, 12)) or 0                  # BITSPIXEL
        planes = int(_gdi32.GetDeviceCaps(hdc, 14)) or 0                # PLANES
        rep["bits_per_pixel"] = bits
        rep["planes"] = planes
        # ВАЖНО: BITSPIXEL — бит НА ПИКСЕЛЬ, а не на канал. 32 бита при PLANES=1 —
        # обычный сурфейс DWM, к 10-битному выводу отношения не имеет (предыдущая
        # версия именно так и врала: depth=32 проходило как «10 бит/HDR»).
        if planes == 3:
            rep["bits_per_channel"] = bits
            rep["deep_color"] = bits >= 10
        else:
            rep["bits_per_channel"] = None
            rep["deep_color"] = None
        try:                                          # MXDC_ENABLE_HDR (110), wingdi.h
            rep["hdr_enabled"] = int(_gdi32.GetDeviceCaps(hdc, 110)) != 0
        except Exception:
            rep["hdr_enabled"] = None
    except Exception:
        pass
    finally:
        if hdc:
            try:
                _user32.ReleaseDC(0, hdc)
            except Exception:
                pass
    hints = []
    if rep.get("remote_session"):
        hints.append("ЭТО УДАЛЁННЫЙ СЕАНС (RDP): Windows в терминальной сессии не отдаёт "
                     "gamma-таблицу, SetDeviceGammaRamp будет отказывать всегда. Запускайте "
                     "программу на том компьютере, ЧЕРЕД ЭКРАНОМ которого вы сидите (см. README, "
                     "раздел «сеанс: удалённый (RDP)»)")
    if rep.get("hdr_enabled"):
        hints.append("HDR включён: при активном HDR/Auto HDR Windows игнорирует gamma-таблицу "
                     "-> Настройки → Дисплей → HDR → Auto HDR: Выкл")
    if rep.get("deep_color"):
        hints.append("режим 10 бит на канал: часть стеков драйверов не даёт программную гамму "
                     "-> выставьте 8 бит в панели NVIDIA/AMD")
    if not hints:
        hints.append("если отказ остался — выключите «Ночной свет»/f.lux (они владеют той же "
                     "таблицей), обновите драйвер GPU: на части сборок программная гамма "
                     "отключена в самом драйвере")
    rep["hints"] = hints
    return rep


def human_env(rep: dict) -> str:
    """Одна строка с окружением для --check и окна."""
    bpc = rep.get("bits_per_channel")
    if bpc:
        col = "%d бит/канал" % bpc
    else:
        col = "%s бит/пиксель, %d планов (точность канала по GDI не видна)" % (
            rep.get("bits_per_pixel", "?"), rep.get("planes", 0))
    hdr = {True: "HDR вкл", False: "HDR выкл"}.get(rep.get("hdr_enabled"), "HDR: не определить")
    return "сеанс: %s | цвет: %s | %s" % (
        "УДАЛЁННЫЙ (RDP)" if rep.get("remote_session") else "локальный", col, hdr)


def probe_gamma_support(ramp: GammaRamp) -> dict:
    """Эмпирическая проверка: ставим заметную таблицу, читаем обратно.

    Нужна потому, что при включённом HDR (Win11 Auto HDR / HDR-монитор)
    Windows игнорирует SetDeviceGammaRamp — юзер бы просто не понял, почему
    «программа не работает». Также ловим случай, когда гамму уже держит
    ночной свет/драйвер.
    """
    if not IS_WINDOWS:
        return {"ok": False, "reason": "not windows"}
    orig = ramp._read()
    # специально с тилтом: при R=G=B перемешанная и планарная раскладки совпадают,
    # и проба бы «прошла» даже с неправильной раскладкой байтов
    test = correction.build_luts(gamma_factor=1.8, tint=(1.12, 1.0, 0.90))
    if not ramp._write(correction.ramp_bytes(test)):
        if orig is not None:
            ramp._write(orig)
        env = gamma_env_report()
        why = "; ".join(env.get("hints", []))
        return {"ok": False, "env": env,
                "reason": "SetDeviceGammaRamp вернул отказ — %s. DC: %s. Что пробовать: %s"
                          % (ramp.error_text(), ramp.source, why)}
    back = ramp._read()
    if orig is not None:
        ramp._write(orig)
    if back is None:
        return {"ok": True, "reason": "установка прошла, чтение недоступно (DC: %s, %d записей на канал)"
                               % (ramp.source, ramp.ramp_mode)}
    a = struct.unpack("<48H", back[:96])          # первые 48 записей = низ красного блока
    b = struct.unpack("<48H", orig[:96]) if orig else (0,) * 48
    delta = sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)   # в единицах 0..65535
    if delta < 500:
        return {"ok": False, "env": gamma_env_report(), "reason":
                "таблица ставится, но не применяется — почти наверняка включён HDR "
                "(Настройки → Дисплей → HDR → Auto HDR = Выкл) или её держит «Ночной свет»"}
    return {"ok": True, "reason": "gamma-таблица применяется (DC: %s, %d записей на канал)"
                               % (ramp.source, ramp.ramp_mode)}


# --------------------------------------------------------------------------
# Хоткеи через поллинг GetAsyncKeyState — без RegisterHotKey, без конфликтов
# с чужими хоткеями, работают поверх Fullscreen/Borderless.
# --------------------------------------------------------------------------
class Hotkeys:
    def __init__(self, bindings: dict[int, str]):
        self._bindings = bindings
        self._down: set[int] = set()

    def poll(self) -> list[str]:
        """Возвращает имена кнопок, которые «нажали» с прошлого вызова.

        `_user32` берём из globals(): в тестах IS_WINDOWS подменяют, не подменяя
        ctypes-ручки, и прямой отсыл к _user32 ронял поток с NameError.
        """
        user = globals().get("_user32")
        if user is None:
            return []
        fired = []
        for vk, name in self._bindings.items():
            pressed = bool(user.GetAsyncKeyState(vk) & 0x8000)
            if pressed and vk not in self._down:
                fired.append(name)
            self._down.discard(vk) if not pressed else None
            if not pressed:
                self._down.discard(vk)
            else:
                self._down.add(vk)
        return fired


# --------------------------------------------------------------------------
# Фокус/наличие игры + яркость монитора
# --------------------------------------------------------------------------
def foreground_process() -> str:
    """Имя exe активного окна ('' если не удалось)."""
    if not IS_WINDOWS:
        return ""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
            return ""
        finally:
            _kernel32.CloseHandle(h)
    except Exception:
        return ""


def is_tarkov_focus() -> bool:
    return foreground_process() in _GAME_PROCESSES


def game_running() -> bool:
    """Через tasklist — без внешних либ. Костыльно, но надёжно."""
    if not IS_WINDOWS:
        return False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, creationflags=0x08000000, timeout=2).stdout.lower()
    except Exception:
        return False
    return any(g in out for g in _GAME_PROCESSES)


def set_monitor_brightness(percent: int | None) -> str:
    """Яркость самой панели (внутренние мониторы ноутбуков). None = прочитать.

    Возвращает человекочитаемый статус. На десктопных мониторах по DDC часто
    недоступно — тогда просто сообщаем об этом, не падая.
    """
    if not IS_WINDOWS:
        return "не Windows"
    if percent is None:
        ps = ("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness"
              ").CurrentBrightness")
    else:
        p = max(0, min(100, int(percent)))
        ps = (f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods"
              f").WmiSetBrightness(1,{p}); 'ok'")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=6,
                           creationflags=0x08000000)
        txt = (r.stdout or r.stderr).strip()
        return txt or "панель не отвечает (DDC недоступен)"
    except Exception as e:
        return f"не вышло: {e}"
