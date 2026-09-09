#!/usr/bin/env python3
"""Помощник самообновления: дождаться закрытия программы → доделать замену → запустить заново.

Его не запускают руками (для этого есть кнопка «Обновить» / `python app\\main.py --update`);
он живёт отдельным процессом ровно столько, сколько нужно, и пишет отчёт в update.log.

    python app/_update_helper.py --wait-pid 1234 [--retry-apply DIR --target DIR]
                                 [--restart-file FILE] [--log FILE] [--max-wait 90]

Почему отдельный процесс: пока приложение живо, Windows не отдаёт подменить
файлы, которые оно держит (собранный exe, .pyd), а «перезапустить самого себя»
из того же процесса нельзя — некому остаться, чтобы дождаться.

Скрипт должен работать на голом Python и не падать молча: любая ошибка — строка
в --log, и процесс выходит с кодом 1 (в окне её видно после перезапуска).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import updater as U                                       # noqa: E402

_LOG_PATH = ""


def log(msg: str) -> None:
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line)                     # под pythonw stdout может быть None, а под
    except Exception:                   # cp1252 — пасть на кириллице: вывод не важен
        pass
    if _LOG_PATH:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def wait_exit(pid: int, max_wait: float = 90.0) -> str:
    """Дождаться выхода процесса. '' = вышел, иначе — почему ушли ждать.

    На Windows ждём через WaitForSingleObject(SYNCHRONIZE): os.kill(pid, 0)
    там не «проверка», а TerminateProcess, то есть убило бы приложение.
    """
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k32.WaitForSingleObject.restype = ctypes.c_ulong
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            SYNCHRONIZE, WAIT_OBJECT_0 = 0x00100000, 0x00000000
            h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if h:
                try:
                    r = k32.WaitForSingleObject(h, int(max_wait * 1000))
                    return "" if r == WAIT_OBJECT_0 else "ожидание прервано (код %d)" % r
                finally:
                    k32.CloseHandle(h)
            return ""                                   # хэндла нет: процесс уже умер или чужой
        except Exception as e:                          # noqa: BLE001
            log("OpenProcess не удался (%s), перехожу на опрос" % e)
    t0 = time.time()
    while time.time() - t0 < max_wait:
        if not _alive(pid):
            return ""
        time.sleep(0.2)
    return "подождал %.0f с и приложение не закрылось" % max_wait


def _alive(pid: int) -> bool:
    """Жив ли процесс. Особый случай — зомби: он уже завершился, но `os.kill(pid, 0)`
    на него отвечает «да» до тех пор, пока родитель не заберёт код возврата, —
    иначе помощник ждал бы целую вечность вместо одной десятой секунды.
    """
    try:                                     # наш ребёнок -> waitpid и забирает статус
        done, _status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass                                   # не наш ребёнок: смотрим со стороны
    except OSError:
        return False
    stat = "/proc/%d/stat" % pid               # Linux: состояние «Z» = мёртв
    try:
        with open(stat, "rb") as f:
            if f.read().rsplit(b")", 1)[-1].split()[0] == b"Z":
                return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                              # чужой процесс: считаем живым, но есть таймаут
    except OSError:
        return False
    return True


def spawn_detached(argv: list, cwd: str) -> bool:
    try:
        kw = {"cwd": cwd or None, "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_PROCESS_GROUP
        subprocess.Popen(argv, **kw)
        return True
    except Exception as e:                                # noqa: BLE001
        log("не смог запустить заново: %s: %s" % (type(e).__name__, e))
        return False


def main(argv=None) -> int:
    global _LOG_PATH
    ap = argparse.ArgumentParser(description="помощник обновления TarkovBright")
    ap.add_argument("--wait-pid", type=int, default=0)
    ap.add_argument("--max-wait", type=float, default=90.0)
    ap.add_argument("--retry-apply", metavar="STAGED", default="",
                    help="каталог со staged-файлами, которые не удалось заменить сразу")
    ap.add_argument("--target", default="", help="куда раскладывать (по умолчанию корень программы)")
    ap.add_argument("--restart-file", default="", help="json с argv/cwd для перезапуска")
    ap.add_argument("--log", default="")
    a = ap.parse_args(argv)
    _LOG_PATH = a.log or U.update_log_path()
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    log("помощник обновления стартовал (pid %d, жду pid %s)" % (os.getpid(), a.wait_pid or 0))

    note = wait_exit(a.wait_pid, a.max_wait)
    if note:
        log(note)

    ok = True
    if a.retry_apply:
        target = a.target or U.ROOT
        try:
            rep = U.apply_update(a.retry_apply, target, backup=False)
            log("повторная замена: файлов %d, ошибок %d" % (len(rep["applied"]), len(rep["errors"])))
            for e in rep["errors"]:
                log("  ! " + e)
            if not rep["applied"]:
                ok = False
            elif not rep["errors"]:
                shutil.rmtree(a.retry_apply, ignore_errors=True)   # staged больше не нужен
        except Exception as e:                            # noqa: BLE001
            ok = False
            log("повторная замена не удалась: %s: %s" % (type(e).__name__, e))

    if a.restart_file:
        try:
            with open(a.restart_file, "r", encoding="utf-8") as f:
                plan = json.load(f)
            os.remove(a.restart_file)
            cmd = list(plan.get("argv") or [])
            if cmd:
                log("перезапускаю: %s" % subprocess.list2cmdline(cmd))
                ok = spawn_detached(cmd, plan.get("cwd") or "") and ok
            else:
                log("в плане перезапуска пустая команда — пропускаю")
        except Exception as e:                            # noqa: BLE001
            log("план перезапуска не прочитан: %s: %s" % (type(e).__name__, e))
    log("готово (%s)" % ("ok" if ok else "есть ошибки"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                                # noqa: BLE001 — тихая смерть хуже
        log("помощник упал: %s: %s" % (type(e).__name__, e))
        try:
            print("update helper failed: %s" % e)
        except Exception:
            pass
        sys.exit(1)
