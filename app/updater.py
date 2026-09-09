"""Самообновление: «Проверить / Обновить» прямо из окна, без ручного скачивания репо.

Путь один и тот же и для клона с git, и для «просто распаковал zip»:

    проверить коммит в GitHub → скачать архив ветки → разложить в папку
    с программой (с бэкапом) → перезапуститься.

Принципы, которые нельзя терять:

* Только stdlib (urllib / zipfile / hashlib / shutil). Приложение обязано
  работать на голом Python, и обновлятор — тем более: он нужен как раз тогда,
  когда с окружением что-то не так.
* Сеть не блокирует интерфейс: все вызовы идут из фонового потока с таймаутом,
  а при отказе в окне появляется понятная строка, а не молчание.
* Обновляются только код и ресурсы. Ваш `%APPDATA%\\TarkovBright\\config.json`,
  скриншоты в `samples/`, каталог `_update/` и сам `.git` не трогаются;
  удалённых файлов нет вообще — если что-то пропало, его вернёт «Откатить».
* Перед записью всё лежит в `_update/staged-<ts>`; применяется по одному
  файлу через os.replace, оригиналы — в `_update/backup-<ts>`.
* Архив из интернета считается враждебным: проверяется раскладка zip,
  отсутствие «/..», абсолютных путей и симлинков, размер и число файлов.

Токен не нужен: репозиторий публичный. Хотите снять лимит 60 запросов/час —
положите PAT в `%APPDATA%\\TarkovBright\\token.txt` или в переменную
`TARKOVBRIGHT_TOKEN` (только чтение, в репозиторий не пишется, в логи не попадает).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
import io

try:                                   # пакет (app/) или голый скрипт — как удобно
    from . import version as V
except ImportError:                    # pragma: no cover - путь для `python app/main.py`
    import version as V

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)            # каталог с программой (= корень репозитория)

API_ROOT = "https://api.github.com"
ZIP_URL = "https://codeload.github.com/{repo}/zip/refs/heads/{branch}"
UA = "%s/%s (+self-update)" % (V.APP_NAME, V.__version__)

UPDATE_DIR = "_update"                 # внутри ROOT: staged-*/ и backup-*/
STATE_FILE = "update.json"             # внутри каталога конфига (%APPDATA%/TarkovBright)

# Что разрешено обновлять. Всё остальное в архиве игнорируется намеренно:
# так обновление не сможет ни удалить ваши файлы, ни притащить мусор.
TRACK_DIRS = ("app", "tools", "tests", "reshade", ".github")
TRACK_ROOT_EXT = (".bat", ".ps1", ".md", ".txt", ".py")
TRACK_ROOT_NAMES = ("LICENSE", "requirements.txt")

# «защита от дурака» при распаковке чужого zip
MAX_FILES = 4000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 96 * 1024 * 1024
TIMEOUT = 12.0
REQUIRED_IN_ARCHIVE = ("app/main.py", "app/engine.py", "app/correction.py", "app/version.py")

_VER_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')


def supports_self_update() -> tuple:
    """Может ли эта копия обновить сама себя. -> (да/нет, почему).

    Собранный PyInstaller'ом onefile-exe — не может осмысленно: код зашит
    внутрь бинарника, и подмена app/*.py рядом с ним ничего не меняет.
    Таким копиям нужен новый exe (GitHub Actions → артефакт), а не патч файлов.
    """
    if getattr(sys, "frozen", False):
        return False, ("собранная .exe сама себя не перепишет: возьми новый "
                       "TarkovBright.exe (GitHub → Actions → build-exe → артефакт, "
                       "или кнопка «Обновить» в .py-версии) и замени файл")
    return True, ""


# --------------------------------------------------------------------------
# мелочи быта
# --------------------------------------------------------------------------
def config_dir() -> str:
    """Тот же каталог, где лежит config.json (engine.config_path())."""
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, V.APP_NAME)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def state_path() -> str:
    return os.path.join(config_dir(), STATE_FILE)


def load_state() -> dict:
    return _read_json(state_path())


def save_state(**kw) -> dict:
    st = load_state()
    st.update(kw)
    try:
        _write_json(state_path(), st)
    except OSError:
        pass                                  # состояние — не критично, не роняем апдейт
    return st


def _token() -> str:
    """Необязательный PAT: env → token.txt в каталоге конфига. Пусто = анонимно."""
    tok = (os.environ.get("TARKOVBRIGHT_TOKEN") or "").strip()
    if not tok:
        p = os.path.join(config_dir(), "token.txt")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    tok = f.read().strip()
            except OSError:
                tok = ""
    return tok


def _headers(extra: dict | None = None) -> dict:
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    tok = _token()
    if tok:
        h["Authorization"] = "Bearer " + tok
    if extra:
        h.update(extra)
    return h


# --------------------------------------------------------------------------
# HTTP (одна точка, чтобы тесты подменяли только её)
# --------------------------------------------------------------------------
def urllib_fetch(url: str, headers: dict | None = None, timeout: float = TIMEOUT):
    """-> (status:int, headers:dict, body:bytes). 304/404 возвращаются статусом,
    а не исключением: для кэша по ETag это нормальный ответ."""
    req = urllib.request.Request(url, headers=headers or _headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.getcode()), dict(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return int(e.code), dict((e.headers or {}).items()), body


def parse_version_from_text(text: str) -> str:
    m = _VER_RE.search(text or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# что стоит локально
# --------------------------------------------------------------------------
def local_git_sha(root: str | None = None) -> str:
    """HEAD клона, если рядом есть .git (читаем файлы, а не зовём git — он
    может не быть в PATH). Иначе ''."""
    git = os.path.join(root or ROOT, ".git")
    try:
        with open(os.path.join(git, "HEAD"), "r", encoding="utf-8", errors="replace") as f:
            head = f.read().strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            p = os.path.join(git, *ref.split("/"))
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    return f.read().strip()
            with open(os.path.join(git, "packed-refs"), "r", encoding="utf-8",
                      errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.endswith(" " + ref) and not line.startswith("#"):
                        return line.split()[0]
        else:
            return head                       # detached HEAD: sha прямо в HEAD
    except Exception:
        return ""
    return ""


def local_fingerprint(root: str | None = None) -> str:
    """Короткий отпечаток кода — «кто мы такие», если .git потерян (скачали zip).

    Только app/*.py: их содержимое и есть то, что меняет поведение. Считается
    лениво и только при необходимости (на ~10 файлов — миллисекунды).
    """
    h = hashlib.sha256()
    d = os.path.join(root or ROOT, "app")
    try:
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                with open(os.path.join(d, name), "rb") as f:
                    h.update(name.encode())
                    h.update(f.read())
    except OSError:
        return ""
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# проверка обновлений
# --------------------------------------------------------------------------
def get_head(fetch=urllib_fetch, repo: str | None = None, branch: str | None = None,
             etag: str = "") -> dict:
    """Один запрос к GitHub: последний коммит ветки (sha, сообщение, дата)."""
    repo = repo or V.REPO
    branch = branch or V.BRANCH
    extra = {"If-None-Match": etag} if etag else {}
    out = {"ok": False, "repo": repo, "branch": branch, "url":
           "https://github.com/%s/tree/%s" % (repo, branch)}
    try:
        status, hdrs, body = fetch("%s/repos/%s/commits/%s" % (API_ROOT, repo, branch),
                                   _headers(extra), TIMEOUT)
    except Exception as e:                                   # noqa: BLE001
        out["error"] = "нет связи с GitHub (%s: %s)" % (type(e).__name__, e)
        out["offline"] = True
        return out
    if status == 304:
        out.update(ok=True, cached=True)
        return out
    if status in (403, 429):
        out["error"] = ("GitHub ограничил анонимные запросы (лимит 60/час на IP). "
                        "Попробуйте позже или положите PAT в TARKOVBRIGHT_TOKEN")
        out["rate_limited"] = True
        return out
    if status != 200:
        out["error"] = "GitHub ответил %d" % status
        return out
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        commit = data.get("commit") or {}
        out.update(ok=True,
                   sha=str(data.get("sha") or ""),
                   message=str(((commit.get("message") or "").splitlines() or [""])[0])[:160],
                   date=str((commit.get("committer") or {}).get("date") or ""),
                   etag=str(hdrs.get("ETag") or ""),
                   cached=False)
    except Exception as e:                                   # noqa: BLE001
        out["error"] = "не разобрал ответ GitHub: %s" % e
    return out


def check(fetch=None, repo: str | None = None, branch: str | None = None,
          root: str | None = None, force: bool = False) -> dict:
    """Знает ли приложение, что на GitHub уже есть новее.

    Сравнение по sha коммита: .git/HEAD (клон) или записанный после последнего
    обновления sha (случай «распаковал zip»). Если не известно ни то ни другое —
    состояние 'unknown': кнопка «Обновить» при этом работает как обычно.
    """
    fetch = fetch or urllib_fetch
    root = root or ROOT
    st = load_state()
    res = {"ok": True, "state": "unknown", "local_version": V.__version__,
           "remote_version": "", "local_sha": "", "remote_sha": "",
           "message": "", "url": "", "from_cache": False}

    if not force:
        age = time.time() - float(st.get("checked_at") or 0)
        if (st.get("state") == "up-to-date" and age < V.CHECK_INTERVAL_H * 3600
                and st.get("remote_sha")):
            res.update(state="up-to-date", from_cache=True,
                       remote_sha=st.get("remote_sha", ""),
                       message="проверено %.1f ч назад" % (age / 3600.0))
            return res

    head = get_head(fetch, repo, branch, etag=str(st.get("etag") or ""))
    res["url"] = head.get("url", "")
    if not head.get("ok"):
        res["ok"] = False
        res["state"] = ("offline" if head.get("offline")
                        else "rate-limited" if head.get("rate_limited") else "error")
        res["message"] = head.get("error", "не удалось проверить")
        if st.get("remote_sha") and head.get("cached") is None:
            res["message"] += "; последняя известная ревизия: " + st["remote_sha"][:8]
        return res

    local = local_git_sha(root) or str(st.get("installed_sha") or "")
    if head.get("cached"):                      # ETag: ничего не изменилось с прошлой проверки
        res.update(state="up-to-date", local_sha=local,
                   remote_sha=str(st.get("remote_sha") or ""),
                   message="обновлений нет (кэш ETag)", from_cache=True)
        return res

    remote = head.get("sha", "")
    res.update(local_sha=local, remote_sha=remote, message=head.get("message", ""),
               date=head.get("date", ""))
    if not local:
        res["state"] = "unknown"
        res["message"] = (res["message"] + " — какая у вас ревизия, неизвестно; "
                          "обновление безопасно (конфиг и скриншоты не трогаются)").strip(" —")
    elif local == remote:
        res["state"] = "up-to-date"
        res["message"] = "обновлений нет"
    else:
        res["state"] = "update-available"
        res["ahead"] = True
        res["message"] = res["message"] or "есть новый коммит"

    save_state(checked_at=time.time(), etag=head.get("etag", ""), remote_sha=remote,
               state=res["state"], remote_version_hint=str(st.get("remote_version_hint") or ""))
    return res


# --------------------------------------------------------------------------
# скачивание и распаковка архива
# --------------------------------------------------------------------------
def download(fetch=None, repo: str | None = None, branch: str | None = None) -> bytes:
    """Архив ветки (zip ~2 МБ). Единственное «тяжёлое» сетевое действие."""
    fetch = fetch or urllib_fetch
    url = ZIP_URL.format(repo=repo or V.REPO, branch=branch or V.BRANCH)
    status, _hdr, body = fetch(url, _headers({"Accept": "application/zip"}), max(TIMEOUT, 45.0))
    if status != 200:
        raise RuntimeError("GitHub отдал архив со статусом %d — попробуйте позже" % status)
    if len(body) > MAX_TOTAL_BYTES:
        raise RuntimeError("архив подозрительно большой (%d байт) — обновляться не буду" % len(body))
    return body


def _safe_member(name: str) -> str | None:
    """Относительный путь внутри staged-дерева или None, если элемент подозрительный."""
    name = (name or "").replace("\\", "/")
    if not name or name.endswith("/"):
        return None
    if os.path.isabs(name) or re.match(r"^[A-Za-z]:", name):
        return None
    parts = []
    for p in name.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            return None                          # zip-slip: наружу из staged
        parts.append(p)
    if len(parts) < 2:                           # всё в архиве лежит в tarkovLight-<branch>/
        return None
    return "/".join(parts[1:])


def stage(blob: bytes, dest: str) -> dict:
    """Распаковывает архив в `dest` (плоско, только разрешённые пути).

    Возвращает {"files": {rel: abspath}, "skipped": int, "total": int}.
    Ни одного файла не пишется до полной проверки содержимого архива.
    """
    os.makedirs(dest, exist_ok=True)
    files: dict[str, str] = {}
    skipped = 0
    total = 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except Exception as e:                                   # noqa: BLE001
        raise RuntimeError("скачанный файл не похож на zip (%s)" % e) from e
    with zf:
        for info in zf.infolist():
            total += 1
            if total > MAX_FILES:
                raise RuntimeError("в архиве больше %d файлов — не ожидаю такого, отмена" % MAX_FILES)
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                skipped += 1                               # симлинк наружу — игнорим
                continue
            rel = _safe_member(info.filename)
            if not rel or not _allowed(rel):
                skipped += 1
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise RuntimeError("файл %s слишком большой для обновления (%d байт)"
                                   % (rel, info.file_size))
            data = zf.read(info)
            if len(data) != info.file_size:
                raise RuntimeError("размер %s в архиве не совпадает" % rel)
            dst = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
            files[rel] = dst
    missing = [r for r in REQUIRED_IN_ARCHIVE if r not in files]
    if missing:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError("в архиве нет %s — это не тот репозиторий, обновляться не буду"
                           % ", ".join(missing))
    return {"files": files, "skipped": skipped, "total": total}


def _allowed(rel: str) -> bool:
    top = rel.split("/", 1)[0]
    if "/" not in rel:
        name = rel.lower()
        return (name.endswith(TRACK_ROOT_EXT) or name in TRACK_ROOT_NAMES)
    return top in TRACK_DIRS


def remote_version(staged: str) -> str:
    p = os.path.join(staged, "app", "version.py")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return parse_version_from_text(f.read())
    except OSError:
        return ""


# --------------------------------------------------------------------------
# план и применение
# --------------------------------------------------------------------------
def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def plan(staged: str, root: str | None = None) -> list:
    """[(rel, "new"|"changed", abspath_в_программе)] — то, что реально изменится."""
    root = root or ROOT
    out = []
    for rel in sorted(os.path.relpath(os.path.join(dp, fn), staged).replace(os.sep, "/")
                      for dp, _d, fs in os.walk(staged) for fn in fs):
        if not _allowed(rel):
            continue
        src = os.path.join(staged, *rel.split("/"))
        dst = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(dst):
            out.append((rel, "new", dst))
        elif _hash_file(src) != _hash_file(dst):
            out.append((rel, "changed", dst))
    return out


def apply_update(staged: str, root: str | None = None, backup: bool = True,
                 only: list | None = None) -> dict:
    """Кладёт файлы из staged в root. Один проход = один каталог backup-<ts>.

    os.replace поверх старого файла: либо целиком новое, либо целиком старое,
    «полускрипта» на полпути не бывает.
    """
    root = root or ROOT
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(root, UPDATE_DIR, "backup-" + stamp)
    items = plan(staged, root)
    if only:
        keep = set(only)
        items = [x for x in items if x[0] in keep]
    rep = {"applied": [], "backed_up": [], "errors": [],
           "backup_dir": backup_dir if backup else ""}
    for rel, kind, dst in items:
        src = os.path.join(staged, *rel.split("/"))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if backup and os.path.isfile(dst):
                bd = os.path.join(backup_dir, *rel.split("/"))
                os.makedirs(os.path.dirname(bd), exist_ok=True)
                shutil.copy2(dst, bd)
                rep["backed_up"].append(rel)
            tmp = dst + ".tbnew"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            rep["applied"].append(rel)
        except PermissionError as e:
            rep["errors"].append("%s: нет прав на запись (%s) — запустите программу "
                                 "от администратора или перенесите папку в %%LOCALAPPDATA%%"
                                 % (rel, e))
        except OSError as e:
            rep["errors"].append("%s: %s" % (rel, e))
    # устаревшие .pyc после замены исходников — лишний мусор
    for d in ("app", "tools"):
        p = os.path.join(root, d, "__pycache__")
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    return rep


def backups(root: str | None = None) -> list:
    d = os.path.join(root or ROOT, UPDATE_DIR)
    try:
        return sorted((x for x in os.listdir(d) if x.startswith("backup-")), reverse=True)
    except OSError:
        return []


def rollback(root: str | None = None, name: str | None = None) -> dict:
    """Вернуть файлы из последней (или названной) копии перед обновлением."""
    root = root or ROOT
    names = backups(root)
    name = name or (names[0] if names else "")
    if not name:
        return {"ok": False, "reason": "бэкапов нет — откатывать нечего"}
    src = os.path.join(root, UPDATE_DIR, name)
    rep = {"ok": True, "from": name, "restored": [], "errors": []}
    for dp, _d, fs in os.walk(src):
        for fn in fs:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            dst = os.path.join(root, *rel.split("/"))
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(p, dst)
                rep["restored"].append(rel)
            except OSError as e:
                rep["errors"].append("%s: %s" % (rel, e))
    rep["ok"] = bool(rep["restored"]) and not rep["errors"]
    if rep["ok"]:
        save_state(installed_sha="", installed_fingerprint="")   # sha больше не известна
    return rep


def prune(root: str | None = None, keep: int = 3) -> int:
    """Оставить только последние `keep` бэкапов, чтобы _update не разрастался."""
    root = root or ROOT
    gone = 0
    for name in backups(root)[keep:]:
        shutil.rmtree(os.path.join(root, UPDATE_DIR, name), ignore_errors=True)
        gone += 1
    for name in sorted(x for x in os.listdir(os.path.join(root, UPDATE_DIR))
                       if x.startswith("staged-"))[keep:]:
        shutil.rmtree(os.path.join(root, UPDATE_DIR, name), ignore_errors=True)
        gone += 1
    return gone


# --------------------------------------------------------------------------
# перезапуск (нужен, только если что-то из файлов оказалось занято)
# --------------------------------------------------------------------------
def restart_command(argv=None) -> list:
    """Чем перезапустить приложение с теми же аргументами.

    Возвращаем именно ТОТ вход, которым запустились (`TarkovBright.pyw`,
    `app/main.py` или собранный exe): если дёргать всегда app/main.py, запуск
    двойным кликом после обновления вернётся к pythonw с консолью и без
    привычного поведения.
    """
    extra = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):                       # собранный exe
        return [sys.executable] + extra
    script = ""
    try:
        script = os.path.abspath(sys.argv[0] or "")
    except OSError:
        script = ""
    if not (script.lower().endswith((".py", ".pyw")) and os.path.isfile(script)):
        script = os.path.join(HERE, "main.py")
    return [sys.executable, script] + extra


def stage_dir(root: str | None = None) -> str:
    d = os.path.join(root or ROOT, UPDATE_DIR, "staged-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(d, exist_ok=True)
    return d


def helper_path() -> str:
    return os.path.join(HERE, "_update_helper.py")


def update_log_path() -> str:
    return os.path.join(config_dir(), "update.log")


def write_restart_plan(root: str | None = None, argv=None, cwd: str | None = None) -> str:
    """Файл с командой перезапуска. Файл, а не аргументы командной строки, —
    чтобы кириллица в путях и кавычки не зависели от того, как cmd склеит строку."""
    d = os.path.join(root or ROOT, UPDATE_DIR)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "restart.json")
    _write_json(path, {"argv": [str(x) for x in (argv or restart_command())],
                       "cwd": cwd or (root or ROOT)})
    return path


def spawn_helper(args: list, log_path: str | None = None) -> bool:
    """Отделить помощника от нас: он дождётся нашего выхода и доделает работу.

    Отдельный процесс нужен потому, что собранный в exe интерпретатор
    (и .pyd рядом с ним) Windows не отдаёт подменить «на живую».
    """
    cmd = [sys.executable, helper_path()] + list(args)
    if getattr(sys, "frozen", False):                       # exe: своего python нет
        cmd = [sys.executable, helper_path()] + list(args)
    log_path = log_path or update_log_path()
    try:
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED|NEW_GROUP|NO_WINDOW
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write("\n=== spawn %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                subprocess.list2cmdline(cmd)))
            lf.flush()
        subprocess.Popen(cmd, **kw)
        return True
    except Exception as e:                                   # noqa: BLE001
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write("spawn failed: %s: %s\n" % (type(e).__name__, e))
        except OSError:
            pass
        return False


def request_restart(root: str | None = None, argv=None, retry_staged: str = "") -> bool:
    """«Закройся и вернись сам». Возвращает True, если помощник стартовал."""
    args = ["--wait-pid", str(os.getpid()), "--log", update_log_path(),
            "--restart-file", write_restart_plan(root, argv)]
    if retry_staged:
        args += ["--retry-apply", retry_staged, "--target", root or ROOT]
    return spawn_helper(args)


# --------------------------------------------------------------------------
# высокоуровневый сценарий (его зовёт и GUI, и CLI)
# --------------------------------------------------------------------------
def perform_update(fetch=None, root: str | None = None, restart: bool = True,
                   repo: str | None = None, branch: str | None = None,
                   progress=None) -> dict:
    """check -> download -> stage -> plan -> apply -> (перезапуск помощником).

    Применяем сразу: .py-файлы Windows не держит открытыми, поэтому перезапуск
    нужен не «чтобы подменить файл», а чтобы новый код начал исполняться.
    Если какой-то файл всё же не отдался (занят/нет прав) — staged остаётся
    живым, и помощник повторит замену после выхода.
    """
    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:                                # noqa: BLE001
                pass
        return msg

    root = root or ROOT
    rep = {"ok": False, "applied": [], "backup_dir": "", "restart": False, "errors": [],
           "state": "", "message": "", "version_from": V.__version__, "version_to": ""}
    say("проверяю GitHub …")
    st = check(fetch=fetch, root=root, force=True, repo=repo, branch=branch)
    rep["state"] = st["state"]
    if st["state"] in ("offline", "error", "rate-limited"):
        rep["message"] = st["message"]
        say(rep["message"])
        return rep

    say("скачиваю архив ветки %s …" % (branch or V.BRANCH))
    try:
        blob = download(fetch, repo, branch)
    except Exception as e:                                   # noqa: BLE001
        rep["message"] = "не скачалось: %s" % e
        say(rep["message"])
        return rep

    d = stage_dir(root)
    say("распаковываю и проверяю архив …")
    try:
        stage(blob, d)
    except Exception as e:                                   # noqa: BLE001
        shutil.rmtree(d, ignore_errors=True)
        rep["message"] = "архив не подошёл: %s" % e
        say(rep["message"])
        return rep
    rep["version_to"] = remote_version(d)
    items = plan(d, root)
    if not items:
        shutil.rmtree(d, ignore_errors=True)
        rep.update(ok=True, message="отличий по файлам нет — у вас уже эта ревизия")
        save_state(installed_sha=st.get("remote_sha", ""), installed_fingerprint=local_fingerprint(root),
                   installed_version=rep["version_to"] or V.__version__)
        say(rep["message"])
        return rep
    say("меняю %d файл(ов), оригиналы — в бэкап …" % len(items))
    applied = apply_update(d, root, backup=True)
    rep.update(applied=applied["applied"], backup_dir=applied["backup_dir"],
               errors=applied["errors"])
    if rep["errors"]:
        say("не заменилось: %s" % "; ".join(rep["errors"][:2]))
    retry = d if rep["errors"] else ""
    if not retry:
        shutil.rmtree(d, ignore_errors=True)
    prune(root)
    if not applied["applied"]:
        rep["message"] = "не обновлено ни одного файла: " + "; ".join(rep["errors"][:3])
        say(rep["message"])
        return rep
    save_state(installed_sha=st.get("remote_sha", ""), installed_fingerprint=local_fingerprint(root),
               installed_version=rep["version_to"] or V.__version__, updated_at=time.time())
    rep["ok"] = True
    rep["message"] = "обновлено файлов: %d (v%s -> v%s)" % (
        len(applied["applied"]), rep["version_from"], rep["version_to"] or "?")
    if rep["errors"]:
        rep["message"] += "; %d файл(ов) занято — доменятся после перезапуска" % len(rep["errors"])
    say(rep["message"])

    if restart:
        rep["restart"] = request_restart(root, retry_staged=retry)
        if not rep["restart"]:
            rep["message"] += "; сам перезапуск не удался — закройте и откройте программу заново"
    return rep


def human(rep: dict) -> str:
    """Одна-две строки для консоли/GUI из отчёта check()."""
    if "state" not in rep:
        rep = check(force=True)
    marks = {"up-to-date": "обновлений нет", "update-available": "ЕСТЬ ОБНОВЛЕНИЕ",
             "unknown": "неизвестно (нет .git и записи о последней ревизии)",
             "offline": "нет связи с GitHub", "rate-limited": "лимит запросов GitHub",
             "error": "ошибка проверки"}
    txt = marks.get(rep.get("state", ""), rep.get("state", ""))
    bits = [txt]
    if rep.get("local_sha") or rep.get("remote_sha"):
        bits.append("локально %s -> на GitHub %s" % ((rep.get("local_sha") or "?")[:8],
                                                     (rep.get("remote_sha") or "?")[:8]))
    if rep.get("message"):
        bits.append(rep["message"])
    if rep.get("url"):
        bits.append(rep["url"])
    return " | ".join(str(b) for b in bits if b)
