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

    _VP = ctypes.c_void_p
    # Прототипы (argtypes+restype) — не косметика. Без argtypes ctypes на Windows
    # приводит Питон-целое к C `int` (32 бита), а HDC/HBITMAP/HANDLE в 64-битном
    # процессе — полноценный указатель: отсюда «OverflowError: int too long to
    # convert» у пользователя на SelectObject (v1.3.2, capture) и молча
    # обрезанный указатель на CreateDCW/GetForegroundWindow без restype.
    _PROTOS = (
        (_user32, "GetDC", [_VP], _VP),
        (_user32, "ReleaseDC", [_VP, _VP], ctypes.c_int),
        (_user32, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
        (_user32, "GetForegroundWindow", [], _VP),
        (_user32, "GetWindowThreadProcessId", [_VP, ctypes.POINTER(wintypes.DWORD)],
         ctypes.c_ulong),
        (_user32, "EnumDisplayDevicesW", [_VP, ctypes.c_uint, _VP, ctypes.c_uint],
         wintypes.BOOL),
        (_gdi32, "CreateDCW", [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, _VP],
         _VP),
        (_gdi32, "DeleteDC", [_VP], ctypes.c_int),
        (_gdi32, "GetDeviceCaps", [_VP, ctypes.c_int], ctypes.c_int),
        (_kernel32, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], _VP),
        (_kernel32, "CloseHandle", [_VP], wintypes.BOOL),
        (_kernel32, "ProcessIdToSessionId", [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
         wintypes.BOOL),
    )
    for _lib, _name, _args, _res in _PROTOS:
        try:
            _fn = getattr(_lib, _name)
            _fn.argtypes = list(_args)
            _fn.restype = _res
        except Exception:                               # noqa: BLE001 — нет символа? зовём как умеем
            pass

    _gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
    _gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL
    _gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
    _gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL


@functools.lru_cache(maxsize=1)
def _session_id():
    """Id сеанса, в котором живёт этот процесс (None — не узнать)."""
    try:
        sid = wintypes.DWORD(0)
        if _kernel32.ProcessIdToSessionId(_kernel32.GetCurrentProcessId(), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:
        pass
    return None


def _console_session_id():
    """Id сеанса, привязанного к физическому монитору (None — не узнать)."""
    try:
        return int(_kernel32.WTSGetActiveConsoleSessionId())
    except Exception:
        return None


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


_ACM_STORE = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers\MonitorDataStore"


def _acm_enabled():
    r"""Auto Color Management (Windows 11) по мониторам: True / False / None.

    Только чтение реестра: HKLM\...\GraphicsDrivers\MonitorDataStore\<монитор>,
    параметр AutoColorManagementEnabled — туда пишет переключатель «Автоматически управлять
    цветом для приложений» (Параметры → Дисплей). Это не догадка «у вас точно ACM»:
    без него отказ SetDeviceGammaRamp на NVIDIA + Win11 24H2 — самый частый случай
    «сеанс локальный, драйвер нормальный, а таблица не ставится и код ошибки 0».
    """
    try:
        import winreg
    except ImportError:
        return None
    vals = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ACM_STORE) as k:
            for i in range(16):
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(k, sub) as sk:
                        vals.append(int(winreg.QueryValueEx(sk, "AutoColorManagementEnabled")[0]))
                except OSError:
                    continue
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers") as g:
            if int(winreg.QueryValueEx(g, "EnableAcmSupportDeveloperPreview")[0]):
                vals.append(1)
    except OSError:
        pass
    return any(vals) if vals else None


def _adapter_line(names) -> str:
    """Адаптеры в одну строку, с «xN» вместо дублей (два активных устройства на
    одном GPU — обычное дело, перечислять их подряд — только путать)."""
    seen = []
    for n in names or ():
        if n not in [x[0] for x in seen]:
            seen.append([n, 0])
        for x in seen:
            if x[0] == n:
                x[1] += 1
    return ", ".join(n if c == 1 else "%s ×%d" % (n, c) for n, c in seen)


def _display_adapters() -> tuple:
    """Имена активных адаптеров вывода (DeviceString из EnumDisplayDevices).

    Нужно, чтобы «драйвер вернул FALSE» можно было объяснить числом, а не догадкой:
    «Microsoft Basic Display Adapter», «...Render Driver», ParsecVAdaptor, IddSampleDriver
    и прочие виртуальные адаптеры gamma-таблицу не отдают в принципе.
    """
    out = []
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
                out.append(str(d.DeviceString))
            i += 1
    except Exception:
        pass
    return tuple(out)


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
        self._from_getdc = True         # чем создан текущий DC: ReleaseDC или DeleteDC

    def get(self):
        if not self.hdc:
            self.next()
        return self.hdc

    def _release(self):
        """DC из GetDC отдаётся ReleaseDC, а из CreateDC — только DeleteDC.
        Перепутать их — тихо словить утечку GDI-объектов (ReleaseDC на чужом DC
        возвращает 0 и ничего не освобождает), поэтому помним происхождение."""
        if not self.hdc:
            return
        try:
            if self._from_getdc:
                _user32.ReleaseDC(0, self.hdc)
            else:
                _gdi32.DeleteDC(self.hdc)
        except Exception:
            pass
        self.hdc = 0

    def adopt(self, hdc, source: str, from_getdc: bool = False) -> None:
        """Принять уже созданный DC (после удачной пробы по имени устройства).

        Помним происхождение: ReleaseDC чужому DC не подходит, а DC из CreateDC
        отдаётся только DeleteDC — иначе тихо ловим утечку GDI-объектов.
        """
        self._release()
        self.hdc = int(hdc or 0)
        self.source = source
        self._from_getdc = bool(from_getdc)

    def next(self) -> bool:
        """Следующий доступный DC; True, если удалось переключиться."""
        if self.hdc:
            self._release()
        if self._dev < 0:
            self._dev = 0
            self.source = "GetDC(весь экран)"
            self._from_getdc = True
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
                self._from_getdc = False
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
    # «а точно ли RDP?» — слова должны опираться на числа, которые человек может
    # перепроверить сам (на v1.3.1 пользователь резонно возразил: «я запускаю прямо
    # на своём ПК»). SESSIONNAME/session_id против SM_REMOTESESSION = повод не
    # утверждать RDP категорично, а показать расхождение.
    try:
        rep["session_name"] = (os.environ.get("SESSIONNAME") or "").strip() or None
    except Exception:
        rep["session_name"] = None
    if rep.get("session_id") is None:
        rep["session_id"] = _session_id()
    rep["console_session_id"] = _console_session_id()
    try:
        rep["adapters"] = list(_display_adapters())
    except Exception:
        rep["adapters"] = []
    sid = rep.get("session_id")
    cid = rep.get("console_session_id")
    same_console = (sid is not None and cid is not None and sid == cid
                    and (rep.get("session_name") or "").lower() == "console")
    rep["remote_confirmed"] = bool(rep.get("remote_session")) and not same_console
    rep["suspect_adapter"] = [a for a in rep.get("adapters") or []
                              if any(k in a.lower() for k in _DUMB_ADAPTERS)]
    try:
        rep["acm"] = _acm_enabled()
    except Exception:
        rep["acm"] = None

    hints = []
    if rep.get("remote_session") and rep.get("remote_confirmed"):
        hints.append("ЭТО УДАЛЁННЫЙ СЕАНС (RDP): Windows в терминальной сессии не отдаёт "
                     "gamma-таблицу, SetDeviceGammaRamp будет отказывать ВСЕГДА — и это не "
                     "сломанный драйвер, и не «не тот ПК». Проверить самой Windows: `query user` "
                     "в cmd (ваш сеанс будет помечен rdp-tcp#N, а не Console). Дальше два пути: "
                     "играть в консольном сеансе (вернуть его на монитор — `tscon %s /dest:console` "
                     "из админского cmd внутри RDP, сам RDP отвалится) или смотреть картинку "
                     "зеркалированием консоли (Moonlight/Parsec/AnyDesk/RustDesk — там сеанс "
                     "остаётся Console, и гамма работает)"
                     % (sid if sid is not None else 1))
    elif rep.get("remote_session"):
        # SM_REMOTESESSION=1 при SESSIONNAME=Console и совпадающем id. Утверждать
        # «вы в RDP» больше нельзя (на v1.3.2 пользователь доказал обратное): флаг
        # оставляем в «доказательстве», а ищем причину в цветном конвейере.
        hints.append("Флаг «удалённый сеанс» (SM_REMOTESESSION=1) стоит, но SESSIONNAME=Console "
                     "и id сеанса %s совпадает с консольным — то есть вы, скорее всего, ПРАВА "
                     "в локальном сеансе, и отказ gamma надо искать не в RDP, а в цветном "
                     "конвейере (ACM/HDR/«Ночной свет»/драйвер). Двойным кликом с рабочего стола "
                     "перезапустить всё равно стоит (процесс мог унаследоваться от чужого "
                     "сеанса), и свериться: `query user`" % (sid,))
    if rep.get("hdr_enabled"):
        hints.append("HDR включён: при активном HDR/Auto HDR Windows игнорирует gamma-таблицу "
                     "-> Настройки → Дисплей → HDR → Auto HDR: Выкл")
    if rep.get("deep_color"):
        hints.append("режим 10 бит на канал: часть стеков драйверов не даёт программную гамму "
                     "-> выставьте 8 бит в панели NVIDIA/AMD")
    if rep.get("acm"):
        hints.append("включено Auto Color Management (Windows 11): цветокоррекцию ведёт "
                     "конвейер Windows, и SetDeviceGammaRamp на части стеков отказывает именно "
                     "молча (код 0) -> Параметры → Система → Дисплей → [ваш монитор] → "
                     "«Автоматически управлять цветом для приложений»: Откл, затем «Пересчитать» "
                     "в диагностике. Посмотреть без интерфейса: reg query 'HKLM\\%s' /s /f "
                     "AutoColorManagementEnabled" % _ACM_STORE)
    if rep.get("suspect_adapter") and not rep.get("remote_session"):
        hints.append("адаптер вывода «%s»: базовый/виртуальный драйвер дисплея gamma-таблицу "
                     "не отдаёт. Поставьте драйвер NVIDIA/AMD/Intel (или отключите виртуальный "
                     "адаптер/«Базовый видеоадаптер Microsoft») — после этого --doctor должен "
                     "показать «сеанс: локальный» и OK" % ", ".join(rep["suspect_adapter"]))
    if not hints:
        hints.append("если отказ остался — выключите «Ночной свет»/f.lux (они владеют той же "
                     "таблицей), обновите драйвер GPU: на части сборок программная гамма "
                     "отключена в самом драйвере")
    rep["hints"] = hints
    return rep


# по этим словам в DeviceString понятно, что гаммы не будет независимо от настроек
_DUMB_ADAPTERS = ("basic", "render driver", "display driver only", "virtual",
                  "iddsample", "parsec", "murrcat", "indirect", "microsoft remote")


def human_evidence(rep: dict) -> str:
    """Сырые доказательства по сеансу и адаптеру — строка для диагностики.

    Нужна, чтобы спор «вы в RDP» / «я сижу перед монитором» решался цифрами,
    которые видно в `query user` и диспетчере устройств, а не нашей интерпретацией.
    """
    bits = []
    if rep.get("session_id") is not None:
        bits.append("сеанс %s" % rep["session_id"])
    if rep.get("console_session_id") is not None:
        bits.append("консольный %s" % rep["console_session_id"])
    if rep.get("session_name"):
        bits.append("SESSIONNAME=%s" % rep["session_name"])
    bits.append("SM_REMOTESESSION=%d" % (1 if rep.get("remote_session") else 0))
    if rep.get("adapters"):
        bits.append("адаптер: %s" % _adapter_line(rep["adapters"]))
    if rep.get("acm") is not None:
        bits.append("ACM=%s" % ("вкл" if rep["acm"] else "выкл"))
    return " | ".join(bits)


def human_env(rep: dict) -> str:
    """Одна строка с окружением для --check и окна."""
    bpc = rep.get("bits_per_channel")
    if bpc:
        col = "%d бит/канал" % bpc
    else:
        col = "%s бит/пиксель, %d планов (точность канала по GDI не видна)" % (
            rep.get("bits_per_pixel", "?"), rep.get("planes", 0))
    hdr = {True: "HDR вкл", False: "HDR выкл"}.get(rep.get("hdr_enabled"), "HDR: не определить")
    if rep.get("remote_session") and rep.get("remote_confirmed", True):
        sess = "УДАЛЁННЫЙ (RDP)"
    elif rep.get("remote_session"):
        sess = "локальный (флаг «удалённый» противоречит SESSIONNAME — см. доказательство)"
    else:
        sess = "локальный"
    name = (" " + rep["session_name"]) if rep.get("session_name") else ""
    acm = " | ACM вкл" if rep.get("acm") else ""
    return "сеанс: %s%s | цвет: %s | %s%s" % (sess, name, col, hdr, acm)


def set_console_utf8() -> bool:
    """Попробовать перевести консоль Windows в UTF-8 (SetConsoleOutputCP(65001)).

    True — консоль теперь читает UTF-8 и печатать можно что угодно. False — не
    Windows либо консоль отказалась: тогда надо печатать её же кодовой
    страницей, но с errors="replace" (см. main.fix_console).
    """
    if not IS_WINDOWS:
        return False
    try:
        k32 = globals().get("_kernel32")        # на случай, что ctypes-ручки не создались
        if k32 is None:
            return False
        ok = bool(k32.SetConsoleOutputCP(65001))
        k32.SetConsoleCP(65001)
        return ok
    except Exception:
        return False


def fix_console() -> str:
    """Не даёт русскому выводу уронить программу в консоли Windows.

    Консоль Windows живёт в OEM-кодовой странице (английская — cp1252, русская —
    cp866), а перенаправленный в файл или трубу вывод — в ANSI. Кириллица (и
    стрелка «→») в cp1252 — это UnicodeEncodeError на первой же напечатанной
    строке: так падали `--selftest` и тесты в CI на windows-latest, и так же
    упадёт `python app\\main.py --check > log.txt` у пользователя.

    Правило: удалось переключить консоль в UTF-8 (или вывод не tty) — пишем
    UTF-8; иначе оставляем кодовую страницу консоли, но errors="replace":
    кириллица видна, а одиночные «→» станут «?». Возвращает, что применили.
    """
    utf8 = set_console_utf8()
    applied = "utf-8" if utf8 else "страница консоли + errors=replace"
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:                      # stdout подменён (тесты) — не трогаем
            continue
        try:
            redirected = not stream.isatty()
        except Exception:
            redirected = True
        try:
            if utf8 or redirected:
                reconf(encoding="utf-8", errors="replace")
                applied = "utf-8"
            else:
                reconf(errors="replace")
        except Exception:
            pass                                # лучше плохой вывод, чем падение
    return applied


def _try_device_dcs(ramp: "GammaRamp", blob: bytes) -> str:
    """Последняя попытка: по одному DC на каждый активный монитор (\\.\\DISPLAYn).

    Смысл: GetDC(NULL) — это DC первичного адаптера. Когда адаптеров два (в логе
    v1.3.2 было «RTX 4070 Ti SUPER ×2»), таблица может отвергаться на первичном и
    приниматься на том, к которому подключён монитор с игрой. Без этого шага
    «драйвер вернул FALSE» выглядит как приговор, хотя достаточно выбрать DC.
    При успехе DC запоминается, чтобы не пересоздавать его на каждый кадр.
    """
    for name in _display_names():
        hdc = 0
        try:
            hdc = int(_gdi32.CreateDCW("DISPLAY", name, None, None) or 0)
        except Exception:                               # noqa: BLE001
            continue
        if not hdc:
            continue
        try:
            words = [int(v) for v in struct.unpack("<768H", blob)]
            buf = (ctypes.c_ushort * len(words))(*words)
            if _gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(buf)):
                ramp._dc.adopt(hdc, "CreateDC(%s)" % name)
                return name
        except Exception:                               # noqa: BLE001
            pass
        try:
            _gdi32.DeleteDC(hdc)
        except Exception:
            pass
    return ""


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
    blob = correction.ramp_bytes(test)
    dev = ""
    applied = bool(ramp._write(blob))
    if not applied:
        # не повезло с DC первичного адаптера — пробуем DC каждого монитора;
        # при успехе _try_device_dcs сам оставляет ramp._dc на том DC, который
        # таблицу принял, и весь рабочий цикл пойдёт через него
        dev = _try_device_dcs(ramp, blob)
        applied = bool(dev)
    if not applied:
        if orig is not None:
            ramp._write(orig)
        env = gamma_env_report()
        why = "; ".join(env.get("hints", []))
        return {"ok": False, "env": env,
                "reason": "SetDeviceGammaRamp вернул отказ — %s. DC: %s (пробовали и DC "
                          "каждого монитора). Что пробовать: %s"
                          % (ramp.error_text(), ramp.source, why)}
    note = ""                                 # обычный путь: DC тот же, что и всегда
    back = ramp._read()
    if orig is not None:
        ramp._write(orig)
    if back is None:
        return {"ok": True, "reason": "установка прошла, чтение недоступно (DC: %s, %d записей "
                                      "на канал)%s" % (ramp.source, ramp.ramp_mode, note)}
    a = struct.unpack("<48H", back[:96])          # первые 48 записей = низ красного блока
    b = struct.unpack("<48H", orig[:96]) if orig else (0,) * 48
    delta = sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)   # в единицах 0..65535
    if delta < 500:
        return {"ok": False, "env": gamma_env_report(), "reason":
                "таблица ставится, но не применяется — почти наверняка включён HDR "
                "(Настройки → Дисплей → HDR → Auto HDR = Выкл) или её держит «Ночной свет»"}
    return {"ok": True, "reason": "gamma-таблица применяется (DC: %s, %d записей на канал)%s"
                               % (ramp.source, ramp.ramp_mode,
                                  "" if not dev else
                                  " — принято только на DC монитора %s, его и держим" % dev)}


# --------------------------------------------------------------------------
# Хоткеи через поллинг GetAsyncKeyState — без RegisterHotKey, без конфликтов
# с чужими хоткеями, работают поверх Fullscreen/Borderless.
# --------------------------------------------------------------------------
class Hotkeys:
    def __init__(self, bindings: dict[int, str]):
        # "none" = клавиша сознательно отвязана; пустой словарь — тоже нормальный случай
        self._bindings = {int(vk): str(name) for vk, name in dict(bindings or {}).items()
                          if str(name) != "none"}
        self._down: set[int] = set()

    def poll(self) -> list[str]:
        """Возвращает имена кнопок, «нажатых» с прошлого вызова (фронт по 0x8000).

        `_user32` берём из globals(): в тестах IS_WINDOWS подменяют, не подменяя
        ctypes-ручки, и прямой отсыл к _user32 ронял поток с NameError.
        """
        user = globals().get("_user32")
        if user is None:
            return []
        fired = []
        for vk, name in self._bindings.items():
            try:
                pressed = bool(user.GetAsyncKeyState(vk) & 0x8000)
            except Exception:
                return []                      # пропал user32 — не роняем рабочий цикл
            if pressed:
                if vk not in self._down:
                    fired.append(name)
                self._down.add(vk)
            else:
                self._down.discard(vk)
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


# --------------------------------------------------------------------------
# «один экземпляр на компьютер»
# --------------------------------------------------------------------------
# Второй запуск — это не «две копии, ничего страшного»: два потока по очереди
# дёргают одну и ту же gamma-таблицу, а «оригинал», который сохранит второй
# экземпляр, на деле уже выкручен первым. После выхода обоих экран остаётся
# с чужими цветами, и выглядит это как «программа всё сломала». Поэтому
# встаём на именованный мьютекс (Windows) или на lock-файл (остальные ОС —
# он же позволяет проверить это в тестах без Windows).
_LOCK_KEEP = None


def _lock_file(tag: str) -> str:
    import tempfile
    return os.path.join(tempfile.gettempdir(), "%s.instance.lock" % tag)


def attach_console() -> bool:
    """Вернуть выводу GUI-подсистемы консоль, из которой программу запустили.

    `TarkovBright.exe`, собранный с --noconsole, стандартных потоков не имеет:
    sys.stdout is None, и `TarkovBright.exe --doctor` в cmd молчит (в файл
    перенаправление работает, потому что тогда хэндель наследуется).
    AttachConsole(ATTACH_PARENT_PROCESS) цепляет родительскую консоль, после
    чего print() снова видно. Не вышло (запуск двойным кликом) — тихо False:
    вызывающий сам решает, показывать окно или писать лог.
    """
    if not IS_WINDOWS or sys.stdout is not None:
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if not kernel32.AttachConsole(0xFFFFFFFF):          # ATTACH_PARENT_PROCESS
            return False
        out = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stdout = out
        sys.stderr = out
        return True
    except Exception:                                       # noqa: BLE001
        return False


def acquire_instance_lock(tag: str = "TarkovBright") -> tuple:
    """-> (получилось: bool, подробно: str). Держим блокировку до выхода."""
    global _LOCK_KEEP
    if _LOCK_KEEP is not None:
        return True, "уже заняли в этом процессе"
    if IS_WINDOWS:
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = wintypes.HANDLE
            h = k32.CreateMutexW(None, False, "Local\\%s.single" % tag)
            err = int(ctypes.get_last_error())
            if not h:
                return True, "CreateMutexW не сработал (код %d) — не мешаем запуску" % err
            if err == 183:                     # ERROR_ALREADY_EXISTS
                k32.CloseHandle(h)
                return False, ("в этой сессии уже запущен TarkovBright "
                               "(именуемый мьютекс Local\\%s.single)" % tag)
            _LOCK_KEEP = ("mutex", h, k32)
            return True, "мьютекс Local\\%s.single" % tag
        except Exception as e:                 # noqa: BLE001
            return True, "проверка не удалась (%s) — не мешаем запуску" % e
    path = _lock_file(tag)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                pid = int((f.read().strip() or "0"))
            if pid > 0 and pid != os.getpid():
                try:
                    os.kill(pid, 0)             # жив? тогда второй экземпляр не нужен
                    return False, ("в этом пользователе уже запущен TarkovBright (pid %d), "
                                   "lock-файл %s" % (pid, path))
                except (ProcessLookupError, PermissionError):
                    pass
                except OSError:
                    pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        _LOCK_KEEP = ("file", path, None)
        return True, "lock-файл %s" % path
    except Exception as e:                     # noqa: BLE001
        return True, "не смог залочиться (%s) — не мешаем запуску" % e


def _open_request_file(basedir: str) -> str:
    return os.path.join(basedir, "showme.request")


def request_show_window(basedir: str) -> bool:
    """Второй запуск при уже бегущем экземпляре: попросить его показать окно.

    Возвращает True, если просьбу удалось записать (значит, бегущее окно её
    увидит и поднимется). Если писать некуда — False, и вызывающий поступает
    как раньше (объясняет «уже запущен»).
    """
    try:
        path = _open_request_file(basedir)
        os.makedirs(basedir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


def take_show_request(basedir: str) -> bool:
    """Бегущее окно спрашивает раз в ~0.2 с: меня зовут показаться?

    True — пришла просьба от второго запуска, просьбу гасим (файл удаляем),
    окно поднимается. Положительный результат только один раз на один запуск.
    """
    path = _open_request_file(basedir)
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------
# автостарт при входе в Windows — только для собранного .exe (у исходников
# нет устойчивого «одного файла», который можно прописать в Run)
# --------------------------------------------------------------------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "TarkovBright"


def autostart_command() -> str | None:
    """Командная строка автозапуска. None — режим, где реестровый Run не нужен:
    не Windows или не собранный .exe (sys.frozen). Возвращая None, мы явно не
    даём кнопке «в автозагрузке» висеть у людей, запускающих из исходников.
    """
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return None
    return '"%s" --minimized' % sys.executable


def autostart_enabled() -> tuple:
    """(включено: bool, подробно: str) — состояние записи в HKCU Run."""
    cmd = autostart_command()
    if cmd is None:
        return False, "автозапуск — только для собранного TarkovBright.exe"
    try:
        import winreg
    except Exception as e:                          # noqa: BLE001
        return False, "winreg недоступен: %s" % e
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY)
    except OSError:
        return False, "раздела автозагрузки нет (HKCU\\%s)" % _RUN_KEY
    try:
        try:
            winreg.QueryValueEx(k, _RUN_VALUE)
            return True, "в HKCU\\%s\\%s" % (_RUN_KEY, _RUN_VALUE)
        except FileNotFoundError:
            return False, "в автозагрузке нет TarkovBright"
    finally:
        winreg.CloseKey(k)


def autostart_set(enabled: bool) -> tuple:
    """(успех: bool, подробно: str). True — добавить в Run, False — убрать."""
    cmd = autostart_command()
    if cmd is None:
        return False, "автозапуск — только для собранного TarkovBright.exe"
    try:
        import winreg
    except Exception as e:                          # noqa: BLE001
        return False, "winreg недоступен: %s" % e
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return False, "нет доступа к разделу автозагрузки"
    try:
        if enabled:
            winreg.SetValueEx(k, _RUN_VALUE, 0, winreg.REG_SZ, cmd)
            return True, "добавлено: %s" % cmd
        try:
            winreg.DeleteValue(k, _RUN_VALUE)
        except FileNotFoundError:
            pass
        return True, "запись автозапуска удалена"
    except OSError as e:
        return False, "не удалось записать реестр: %s" % e
    finally:
        winreg.CloseKey(k)


def release_instance_lock() -> None:
    global _LOCK_KEEP
    kind, obj, api = _LOCK_KEEP or (None, None, None)
    _LOCK_KEEP = None
    try:
        if kind == "mutex" and api is not None:
            api.CloseHandle(obj)
        elif kind == "file" and obj and os.path.exists(obj):
            os.remove(obj)
    except Exception:
        pass


# --------------------------------------------------------------------------
# что за сеанс/экран — для диагностики (окно «Диагностика» и --doctor)
# --------------------------------------------------------------------------
def session_report() -> dict:
    """Коротко: ОС, сеанс, дисплейный стек. Только чтение, ничего не меняем."""
    rep = {"platform": sys.platform, "python": sys.version.split()[0],
           "windows": IS_WINDOWS}
    if not IS_WINDOWS:
        rep["note"] = "не Windows: gamma-таблица не ставится, приложение в режиме расчёта"
        return rep
    try:
        v = sys.getwindowsversion()
        rep["os"] = "Windows %d.%d build %d" % (v.major, v.minor, v.build)
    except Exception:
        pass
    try:
        rep["remote_session"] = bool(_user32.GetSystemMetrics(78))    # SM_REMOTESESSION
    except Exception:
        rep["remote_session"] = None
    try:
        rep["terminal_services"] = bool(_user32.GetSystemMetrics(40))  # SM_SERVERR2
    except Exception:
        pass
    sid = _session_id()
    if sid is not None:
        rep["session_id"] = sid
    try:
        rep["display_names"] = list(_display_names())
    except Exception:
        pass
    rep["admin"] = None
    try:
        rep["admin"] = bool(_shell_is_admin())
    except Exception:
        pass
    return rep


def _shell_is_admin() -> bool:
    """ctypes-вариант «мы админ?», без PowerShell (нужен только для подсказок)."""
    try:
        return bool(_user32.IsUserAnAdmin())
    except Exception:
        return False
