#!/usr/bin/env pythonw
"""Tarkov Bright — ОДИН ЗАПУСК ДВОЙНЫМ КЛИКОМ.

Двойной клик по этому файлу = окно с ползунками, хоткеи F7–F10 и кнопка
«Обновить». Больше ничего знать не нужно: pip не обязателен, консоль не нужна,
файлы репозиторий скачивать повторно не надо — за это отвечает «Обновить».

Что тут вообще есть, кроме одной строчки запуска:

* `app/` подкладывается в sys.path — приложение работает и как пакет, и как
  голые скрипты (тот же инвариант, что во всём проекте);
* всё, что упало ДО появления окна (нет Tk, битый конфиг, отсутствующий файл),
  не исчезает молча: текст ошибки пишется в `%APPDATA%\\TarkovBright\\error.log`
  и показывается диалогом. Для `.pyw` консоли нет, и без этого двойной клик
  выглядел бы как «программа не запускается вообще»;
* версия и путь, где живёт код, печатаются в лог — по ним видно, какая ревизия
  у вас сейчас и не лежит ли копия в двух местах сразу.

Есть Python-ассоциации Windows сломаны (файл открывается «не тем»), запускайте
`Start-TarkovBright.bat` — он сам найдёт Python, который реально работает.
"""
from __future__ import annotations

import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


def _log_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "TarkovBright")


def log(msg: str) -> None:
    """Писать всегда, даже если stdout нет (pythonw) и даже если stdout сломан."""
    try:
        os.makedirs(_log_dir(), exist_ok=True)
        with open(os.path.join(_log_dir(), "error.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass
    try:
        if sys.stdout is not None:
            print(msg)
    except Exception:
        pass


def show_error(text: str) -> None:
    """Диалог человеку. TARKOVBRIGHT_QUIET=1 — только лог: модальное окно на
    безголовом Windows-раннере CI вешает процесс, и тест ждёт тайм-аут."""
    if os.environ.get("TARKOVBRIGHT_QUIET") == "1":
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("Tarkov Bright — не запустилось", text)
        r.destroy()
        return
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "Tarkov Bright", 0x10)
        except Exception:
            pass


def hook_excepthook() -> None:
    """Незакрытое исключение в любом потоке — в лог (иначе в .pyw оно невидимо)."""
    def hook(kind, val, tb):
        text = "".join(traceback.format_exception(kind, val, tb))
        log("необработанное исключение:\n" + text)
    sys.excepthook = hook
    try:
        import threading

        def thook(args):
            hook(args.exc_type, args.exc_value, args.exc_traceback)
        threading.excepthook = thook
    except Exception:
        pass


def main() -> int:
    hook_excepthook()
    if sys.version_info[:2] < (3, 9):
        msg = ("Нужен Python 3.9 или новее, а запускает меня %s.\n\nПоставьте актуальный: "
               "winget install -e --id Python.Python.3.12\n(на установщике включите "
               "«Add python.exe to PATH»)." % sys.version.split()[0])
        log("неподходящий python: " + sys.version.replace("\n", " "))
        show_error(msg)
        return 2
    try:
        import main as app_main                          # app/main.py (он же чинит консоль)
    except Exception:
        text = traceback.format_exc()
        log("не смог импортировать app/main.py:\n" + text)
        show_error("Не получилось запустить Tarkov Bright:\n\n" + text.strip().splitlines()[-1]
                   + "\n\nПолный текст — в %s." % os.path.join(_log_dir(), "error.log"))
        return 1
    try:
        import version as V
        log("запуск: v%s из %s (python %s)" % (V.__version__, HERE, sys.version.split()[0]))
    except Exception:
        log("запуск из " + HERE)
    try:
        import windows as W                              # кириллица в консоли: cp1252 боится print
        W.fix_console()
    except Exception:
        pass
    try:
        return int(app_main.main() or 0)
    except SystemExit as e:                              # argparse/--selftest: это не ошибка
        return int(e.code or 0) if isinstance(e.code, (int, type(None))) else 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        text = traceback.format_exc()
        log("исключение в рантайме:\n" + text)
        show_error("Tarkov Bright упал во время работы:\n\n" + text.strip().splitlines()[-1]
                   + "\n\nГамму программа успела вернуть (или вернёт --restore):\n"
                   "python app\\main.py --restore\n\nПолный текст — в %s."
                   % os.path.join(_log_dir(), "error.log"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
