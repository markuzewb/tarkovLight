"""python3 tests/test_resident.py — «один файл живёт в фоне» и надёжность захвата.

Проверяется чистая логика, которой не нужен ни Windows, ни Tk, ни дисплей:

* wake-файл «покажи окно»: второй запуск пишет просьбу, бегущее окно её забирает
  ровно один раз (это механизм, которым однофайловый .exe, свёрнутый в фон при
  закрытии, снова выводится на передний план по повторному запуску);
* автостарт: из исходников / не-Windows честно отказывает (реестр HKCU Run имеет
  смысл только для собранного .exe) и не обещает, что не сможет сделать;
* кэширование списка мониторов в mss-бэкенде захвата: не дёргаем ОС на каждый
  кадр, но перечитываем при «горячем» подключении монитора;
* новые ключи конфига переживают sanitize (болевой инвариант HANDOFF §5 #12).
"""
from __future__ import annotations
import os, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import windows as W            # noqa: E402
import capture as Cp           # noqa: E402
import engine as E             # noqa: E402
W.fix_console()                # русский print не должен падать в cp1252-консоли

FAILS = []


def check(cond, msg, extra=""):
    print(("  ok   " if cond else "  FAIL ") + msg + (f"   [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(msg)


# ---------------------------------------------------------------------------
print("== «покажи окно» (второй запуск -> свёрнутое окно в фон) ==")
base = tempfile.mkdtemp(prefix="tbwake-")
check(W.take_show_request(base) is False, "без просьбы окно не поднимается")
check(W.request_show_window(base) is True, "второй запуск пишет просьбу показать окно")
check(W.take_show_request(base) is True, "бегущее окно просьбу увидело")
check(W.take_show_request(base) is False, "просьба гасится, а не висит вечно")
# каталог, куда писать, может отсутствовать — создаём на лету
sub = os.path.join(base, "a", "b")
check(W.request_show_window(sub) is True, "каталог создаётся, если его ещё нет")
check(W.take_show_request(sub) is True, "просьба из свежесозданного каталога читается")

print("== автостарт: только для собранного .exe ==")
check(W.autostart_command() is None, "из исходников команды автозапуска нет (не обещаем реестр)")
en, why = W.autostart_enabled()
check(en is False and "собранного" in why, "autostart_enabled() честно отказывает не-frozen",
      why[:60])
ok, why2 = W.autostart_set(True)
check(ok is False and why2, "autostart_set() тоже не трогает реестр не-frozen")

print("== mss: список мониторов кэшируется, но перечитывается при хотплаге ==")
import types as _t


class FakeShot:
    def __init__(self, n, m):
        self.width, self.height = n * 100, m * 100
        self.bgra = bytes(self.width * self.height * 4)


class FakeSct:
    """Считает, сколько раз код реально спросил ОС про список мониторов."""

    def __init__(self, nmon):
        self.reads = 0
        self._monitors = [("all", 0, 0, 0, 0)] + [("mon%d" % i, i, 0, 100, 100)
                                                  for i in range(nmon)]

    @property
    def monitors(self):
        self.reads += 1
        return list(self._monitors)

    def grab(self, mon):
        return FakeShot(len(self._monitors), 1)


def _mk_sct(nmon):
    sct = FakeSct(nmon)
    g = _t.SimpleNamespace(_sct=sct, monitor=1, _mon_cache=None, _mon_ts=0.0,
                           last_error="")
    return g, sct


def _age(g, by):
    """«Состарить» кэш так, будто с последнего чтения прошло by секунд."""
    g._mon_ts = time.monotonic() - by


# один Grabber один монитор: ОС про список мониторов спрашиваем один раз
g, sct = _mk_sct(3)
fr = Cp._grab_mss(g)
check(fr is not None and sct.reads == 1, "первый кадр: кэш заполнен за 1 запрос",
      "reads=%d" % sct.reads)
for _ in range(5):
    Cp._grab_mss(g)
check(sct.reads == 1, "следующие кадры список мониторов НЕ перечитывают",
      "reads=%d" % sct.reads)

# горячее подключение/отключение: когда кэш «состарился», список перечитывается
g2, sct2 = _mk_sct(2)               # мониторы 1 и 2; выбран 2-й
g2.monitor = 2
check(Cp._grab_mss(g2) is not None and sct2.reads == 1, "кэш заполнен (монитор #2)")
sct2._monitors = [("all", 0, 0, 0, 0), ("mon1", 1, 0, 100, 100)]   # монитор 2 отключили
_age(g2, Cp._MON_REFRESH_S + 1)     # прошло > лимита -> пора перечитать
fr2 = Cp._grab_mss(g2)
check(fr2 is not None and sct2.reads == 2, "монитор пропал — список перечитан 1 раз",
      "reads=%d" % sct2.reads)
check(Cp._grab_mss(g2) is not None and sct2.reads == 2,
      "после перечитывания снова кэш (не читаем каждый кадр)")

# монитора нет вовсе -> честный None + текст (а не падение)
g3, sct3 = _mk_sct(1)
g3._mon_cache = [("all", 0, 0, 0, 0)]        # только «весь экран», мониторов нет
g3._mon_ts = time.monotonic()                # кэш «свежий» — не перечитываем
g3.monitor = 5
Cp._grab_mss(g3)
check(g3.last_error and "монитора" in g3.last_error, "нет такого монитора — текст, а не падение",
      g3.last_error[:40])

print("== новые ключи конфига переживают sanitize (инвариант #12) ==")
for key in ("minimize_on_close", "autostart"):
    check(key in E.DEFAULT_CONFIG and isinstance(E.DEFAULT_CONFIG[key], bool),
          "ключ %r есть в DEFAULT_CONFIG и это bool" % key)
cfg = dict(E.DEFAULT_CONFIG)
cfg["minimize_on_close"] = "да"
cfg["autostart"] = "off"
warns = E.sanitize(cfg)
check(cfg["minimize_on_close"] is True and cfg["autostart"] is False,
      "строки-флаги поняты как да/нет", "| ".join(warns))
cfg2 = {k: v for k, v in E.DEFAULT_CONFIG.items()}
cfg2.pop("autostart")
fixed = E.sanitize(cfg2)
check("autostart" in cfg2 and "autostart" in " ".join(fixed),
      "отсутствующий ключ возвращается по умолчанию и помечается", cfg2["autostart"])

print()
if FAILS:
    print(f"ПРОВАЛЕНО {len(FAILS)}:"); [print(" -", f) for f in FAILS]; sys.exit(1)
print("Проверки «фонового .exe» и надёжности захвата пройдены")
