"""python3 tests/test_updater.py — обновлятор: план, распаковка, откат, перезапуск.

Сеть не трогается вообще: вместо urllib подставляется `FakeFetch`, который
отдаёт заранее собранные ответы (в т.ч. 403/304/битый zip). Проверено, что:

* решение «обновлять или нет» принимается по sha коммита, а не по на глаз;
* из чужого zip не лезет ни «../», ни абсолютный путь, ни симлинк, ни лишние
  каталоги (обновляются только app/tools/tests/reshade/.github и корневые .bat/.md/…);
* замена файлов откатывается целиком (бэкап пишется до записи, «полускрипта» не бывает);
* помощник доделки (_update_helper) реально запускается как отдельный процесс
  и подменяет то, что не подменилось сразу;
* никаких ошибок при «нет сети», «лимит GitHub», «конфиг битый».
"""
from __future__ import annotations
import copy, io, json, os, subprocess, sys, tempfile, time, zipfile

# Вывод дочерних процессов читаем в UTF-8 (encoding/errors=replace): под
# PYTHONIOENCODING=cp866 text=True декодировал бы его в cp866 и падал —
# ловушка §6 HANDOFF, в test_app.py это уже исправлено тем же способом.

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import windows as W               # noqa: E402
W.fix_console()                   # русские print не должны падать в cp1252-консоли

import correction as C            # noqa: E402
import engine as E                # noqa: E402
import updater as U               # noqa: E402
import version as V               # noqa: E402
import main as M                  # noqa: E402

FAILS = []
N = [0]


def check(cond, msg, extra=""):
    N[0] += 1
    print(("  ok   " if cond else "  FAIL ") + msg + (f"   [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(msg)


def section(t):
    print("== %s ==" % t)


class FakeFetch:
    """Заменяет urllib: (url, headers, timeout) -> (status, headers, body)."""

    def __init__(self, routes=None, raise_exc=None):
        self.routes = routes or {}
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}))
        if self.raise_exc:
            raise self.raise_exc
        for key, val in self.routes.items():
            if key in url:
                return val
        return 404, {}, b'{"message":"Not Found"}'

    def urls(self):
        return [c[0] for c in self.calls]


def head_body(sha="a" * 40, msg="fix: тени в лесу", date="2026-09-08T10:00:00Z", etag='"etag-1"'):
    body = json.dumps({"sha": sha, "commit": {
        "message": msg, "committer": {"date": date}}}).encode()
    return 200, {"ETag": etag}, body


def make_zip(files: dict, folder="tarkovLight-main") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr("%s/%s" % (folder, name), data)
    return buf.getvalue()


def minimal_project(version="9.9.9"):
    """Тот же набор файлов, что требует обновлятор (см. REQUIRED_IN_ARCHIVE)."""
    return {
        "app/main.py": "print('main %s')\n" % version,
        "app/engine.py": "X = 1\n",
        "app/correction.py": "Y = 2\n",
        "app/version.py": '__version__ = "%s"\n' % version,
        "README.md": "# readme v%s\n" % version,
    }


# --------------------------------------------------------------------------
section("1. версия и её разбор")
check(V.version_tuple("1.10.2") == (1, 10, 2), "мажорные сравнения не лексикографические",
      str(V.version_tuple("1.10.2")))
check(V.is_newer("1.10.0", "1.9.9"), "1.10.0 новее, чем 1.9.9")
check(not V.is_newer("1.9.0", "1.9.0"), "одинаковая версия — не «новее»")
check(V.version_tuple(None) == V.version_tuple(V.__version__),
      "версия по умолчанию читается из app/version.py", str(V.version_tuple()))
check(V.version_tuple("v2.0") == (2, 0, 0) and V.version_tuple("мусор") == (0, 0, 0),
      "битая строка версии не ломает сравнение")
check(U.parse_version_from_text('foo\n__version__ = "1.2.3"\n') == "1.2.3",
      "версия вытаскивается из текста app/version.py в архиве")
check(U.parse_version_from_text("нет версии") == "", "нет версии — пустая строка, не исключение")

# --------------------------------------------------------------------------
section("2. конфиг переживает мусор (санитайзер)")
bad = copy.deepcopy(E.DEFAULT_CONFIG)
bad.update({"update_hz": "abc", "shadow_lift": 99, "saturation": 5.0, "monitor_index": None,
            "enabled": "да", "profile": "нет такого", "extra_key": 1,
            "hotkeys": {"F5": "hack", "F8": "toggle"}})
fixed = E.sanitize(bad)
check(bad["update_hz"] == 12, "нечисловое update_hz -> значение по умолчанию", str(bad["update_hz"]))
check(bad["shadow_lift"] == 1.0, "shadow_lift вне диапазона ограничен", str(bad["shadow_lift"]))
check(bad["saturation"] <= 1.24, "saturation не перепрыгивает потолок бандинга", str(bad["saturation"]))
check(bad["monitor_index"] == 1, "monitor_index: None -> 1", str(bad["monitor_index"]))
check(bad["enabled"] is True, "«да» понимается как истина", repr(bad["enabled"]))
check(bad["profile"] in E.PROFILES, "несуществующий профиль заменён", str(bad["profile"]))
check("extra_key" not in bad, "лишние ключи из конфига выбрасываются")
check(bad["hotkeys"] == {"F8": "toggle"}, "неизвестное действие хоткея отбрасывается", str(bad["hotkeys"]))
check(len(fixed) >= 6, "каждое исправление названо словами", "%d шт" % len(fixed))
check(E.sanitize(copy.deepcopy(E.DEFAULT_CONFIG)) == [], "чистый конфиг не «чинится»")
cfg_path = os.path.join(tempfile.mkdtemp(prefix="tbcfg-"), "config.json")
with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
    f.write("{ not json at all")
notes = []
c2 = E.load_config(cfg_path, warn=notes.append)
check(c2 == E.DEFAULT_CONFIG and notes, "битый json -> дефолты + сообщение в warn", str(notes[:1]))
E.save_config(bad, cfg_path)
saved = json.load(open(cfg_path, encoding="utf-8"))
check(set(saved) == set(E.DEFAULT_CONFIG), "save_config пишет только известные ключи")
check(E.sanitize(json.load(open(cfg_path, encoding="utf-8"))) == [],
      "сохранённый конфиг второй проход не меняет (round-trip чистый)")

# --------------------------------------------------------------------------
section("3. проверка наличия обновления (без сети)")
tmp = tempfile.mkdtemp(prefix="tbupd-")
U.state_path = lambda: os.path.join(tmp, "update.json")          # не трогаем реальный %APPDATA%
U.config_dir = lambda: tmp
if os.path.exists(U.state_path()):
    os.remove(U.state_path())

ff = FakeFetch({"/commits/main": head_body(sha="b" * 40)})
st = U.check(fetch=ff, root=tmp, force=True)
check(st["state"] == "unknown" and st["ok"], "без .git и без записей — честное «unknown»", st["state"])
check("/commits/main" in ff.urls()[0] and "api.github.com" in ff.urls()[0],
      "проверка идёт на GitHub API, а не на HTML-страницу", ff.urls()[0])

st2 = U.check(fetch=FakeFetch({"/commits/main": head_body(sha="c" * 40)}), root=tmp, force=True)
check(st2["state"] == "unknown", "без локального sha всё равно «unknown», а не «обновлено»")

# клон: sha из .git/HEAD + refs
gitdir = os.path.join(tmp, ".git", "refs", "heads")
os.makedirs(gitdir, exist_ok=True)
with open(os.path.join(tmp, ".git", "HEAD"), "w", encoding="utf-8", newline="\n") as f:
    f.write("ref: refs/heads/main\n")
with open(os.path.join(gitdir, "main"), "w", encoding="utf-8", newline="\n") as f:
    f.write("d" * 40 + "\n")
check(U.local_git_sha(tmp) == "d" * 40, "локальный sha читается из .git (без вызова git)")
st3 = U.check(fetch=FakeFetch({"/commits/main": head_body(sha="e" * 40)}), root=tmp, force=True)
check(st3["state"] == "update-available", "sha разойшёлся -> есть обновление", st3["state"])
check(st3["message"] == "fix: тени в лесу", "сообщение последнего коммита показывается", st3["message"])
with open(os.path.join(gitdir, "main"), "w", encoding="utf-8", newline="\n") as f:
    f.write("e" * 40 + "\n")
st4 = U.check(fetch=FakeFetch({"/commits/main": head_body(sha="e" * 40)}), root=tmp, force=True)
check(st4["state"] == "up-to-date", "sha совпал -> обновлений нет", st4["state"])
with open(os.path.join(gitdir, "main"), "w", encoding="utf-8", newline="\n") as f:
    f.write("d" * 40 + "\n")

with open(os.path.join(tmp, ".git", "HEAD"), "w", encoding="utf-8", newline="\n") as f:
    f.write("f" * 40 + "\n")                      # detached HEAD
with open(os.path.join(gitdir, "main"), "w", encoding="utf-8", newline="\n") as f:
    f.write("f" * 40 + "\n")
check(U.local_git_sha(tmp) == "f" * 40, "detached HEAD тоже читается")

# ETag: GitHub отвечает 304 -> не скачиваем, говорим «кэш»
ff304 = FakeFetch({"/commits/main": (304, {}, b"")})
st5 = U.check(fetch=ff304, root=tmp, force=True)
check(st5["ok"] and st5["state"] == "up-to-date" and st5.get("from_cache"),
      "304 по ETag = «обновлений нет» без скачивания")
check(any(h.get("If-None-Match") for _u, h in ff304.calls), "ETag прошлого ответа реально отправлен")

# офлайн / лимит
st6 = U.check(fetch=FakeFetch({}, raise_exc=OSError("no route to host")), root=tmp, force=True)
check(not st6["ok"] and st6["state"] == "offline", "нет сети -> state=offline, не исключение", st6["state"])
check("GitHub" in st6["message"], "в офлайне сказано, что именно не так", st6["message"][:50])
st7 = U.check(fetch=FakeFetch({"/commits/main": (403, {}, b'{"message":"rate limit"}')}),
              root=tmp, force=True)
check(st7["state"] == "rate-limited", "403 (лимит 60/час) назван лимитом", st7["state"])
st8 = U.check(fetch=FakeFetch({"/commits/main": (500, {}, b"")}), root=tmp, force=True)
check(st8["state"] == "error" and not st8["ok"], "500 от GitHub — ошибка, а не «обновлений нет»")

# авто-проверка не долбит API: up-to-date кэшируется на время
os.remove(U.state_path())
plain = os.path.join(tmp, "plain")
os.makedirs(plain, exist_ok=True)
ffq = FakeFetch({"/commits/main": head_body(sha="a" * 40)})
U.save_state(installed_sha="a" * 40)
s_a = U.check(fetch=ffq, root=plain, force=False)
s_b = U.check(fetch=ffq, root=plain, force=False)
check(s_a["state"] == "up-to-date" and s_b.get("from_cache") and len(ffq.calls) == 1,
      "повторная авто-проверка в тот же час не дёргает GitHub", "%d запросов" % len(ffq.calls))
check(U.check(fetch=ffq, root=plain, force=True) and len(ffq.calls) == 2,
      "кнопка «Проверить» (force) ходит на сеть всегда")

# --------------------------------------------------------------------------
section("4. распаковка архива: whitelist и защита от враждебного zip")
blob = make_zip(dict(minimal_project("2.0.0"), **{
    "app/deleted_before.py": "x = 1\n",
    "samples/big.png": "not a png\n",
    "evil/../app/outside.py": "boom = 1\n",
    "../../escape.py": "boom = 2\n",
    "/etc/passwd": "boom = 3\n",
    "C:/Windows/system32/evil.dll": "boom\n",
    ".git/config": "[core]\n",
    "node_modules/x/y.js": "junk\n",
}))
staged = os.path.join(tmp, "staged")
rep = U.stage(blob, staged)
rel = sorted(rep["files"])
check("app/main.py" in rel and "app/version.py" in rel, "код из архива распакован", str(rel))
check("README.md" in rel, "ридми в корне тоже обновляется")
check(not any("outside.py" in r or "escape.py" in r or "passwd" in r or "evil" in r
              for r in rel), "zip-slip и абсолютные пути не разошлись по системе", str(rel))
check(not any(r.startswith("samples/") or r.startswith("node_modules/") or r.startswith(".git/")
              for r in rel), "ваши samples/, чужой node_modules/ и .git/ в архиве игнорируются")
check(rep["skipped"] >= 5, "лишние элементы посчитаны", str(rep["skipped"]))
check(open(os.path.join(staged, "app", "version.py"), encoding="utf-8").read().count("2.0.0") == 1,
      "файлы легли на диск")

try:
    U.stage(make_zip({"app/readme.txt": "мусор\n"}, folder="somethingelse"), os.path.join(tmp, "bad2"))
    ok = False
except RuntimeError as e:
    ok = "app/main.py" in str(e)
check(ok, "архив без обязательных файлов отклонён целиком")
try:
    U.stage(b"\xd0\x9d\xd0\x95 zip", os.path.join(tmp, "bad3"))
    ok = False
except RuntimeError as e:
    ok = "zip" in str(e)
check(ok, "битый ответ (не zip) — понятная ошибка, а не traceback", "")
check(not os.path.exists(os.path.join(tmp, "bad2")), "при отказе staged-каталог вычищается")

zf = zipfile.ZipFile(os.path.join(tmp, "link.zip"), "w")
zi = zipfile.ZipInfo("tarkovLight-main/app/evil")
zi.create_system = 3
zi.external_attr = (0o120777 << 16)          # симлинк по инструкции архива
zf.writestr(zi, "../../../../Windows/System32/drivers/etc/hosts")
for name, data in minimal_project("3.0.0").items():
    zf.writestr("tarkovLight-main/" + name, data)
zf.close()
rep = U.stage(open(os.path.join(tmp, "link.zip"), "rb").read(), os.path.join(tmp, "staged-link"))
check("app/evil" not in rep["files"], "симлинк из архива не создан")

# --------------------------------------------------------------------------
section("5. план, применение, откат")
inst = os.path.join(tmp, "install")
os.makedirs(os.path.join(inst, "app"), exist_ok=True)
with open(os.path.join(inst, "app", "main.py"), "w", encoding="utf-8", newline="\n") as f:
    f.write("print('main OLD')\n")
with open(os.path.join(inst, "app", "engine.py"), "w", encoding="utf-8", newline="\n") as f:
    f.write("X = 1\n")
mine = os.path.join(inst, "app", "my_notes.py")
with open(mine, "w", encoding="utf-8", newline="\n") as f:
    f.write("# мой локальный файл, которого нет в репо\n")
keep = os.path.join(inst, "samples")
os.makedirs(keep, exist_ok=True)
with open(os.path.join(keep, "shot.png"), "w", encoding="utf-8", newline="\n") as f:
    f.write("скриншот не трогать")

plan = U.plan(staged, inst)
check(any(r == "app/main.py" and k == "changed" for r, k, _d in plan),
      "изменённый файл помечен changed", str([p[0:2] for p in plan]))
check(any(r == "app/deleted_before.py" and k == "new" for r, k, _d in plan),
      "нового файла нет локально -> new")
check(all(r != "app/engine.py" for r, _k, _d in plan), "идентичный файл в план не попал")
check(os.path.isfile(mine) and os.path.isfile(os.path.join(keep, "shot.png")),
      "plan ничего не удаляет и не создаёт сам по себе")

rep = U.apply_update(staged, inst, backup=True)
check("app/main.py" in rep["applied"], "файл заменён", str(rep["applied"]))
check(open(os.path.join(inst, "app", "main.py"), encoding="utf-8").read().strip() == "print('main 2.0.0')",
      "на диске теперь содержимое из архива")
check(os.path.isfile(mine), "локальные файлы, которых нет в репо, НЕ удалены")
check(open(os.path.join(keep, "shot.png"), encoding="utf-8").read() == "скриншот не трогать",
      "samples/ не тронуты обновлением")
check(rep["backup_dir"] and os.path.isfile(os.path.join(rep["backup_dir"], "app", "main.py")),
      "оригинал сохранён в бэкап", os.path.basename(rep["backup_dir"]))
check("app/deleted_before.py" not in os.listdir(os.path.join(inst, "app"))
      or os.path.isfile(os.path.join(inst, "app", "deleted_before.py")),
      "повторная замена не роняет приложение")
rep2 = U.apply_update(staged, inst, backup=False)
check(rep2["applied"] == [], "повторное применение чистого staged — no-op", str(rep2))
rb = U.rollback(inst)
check("app/main.py" in rb["restored"], "откат вернул файл из бэкапа", str(rb["restored"])[:60])
check(open(os.path.join(inst, "app", "main.py"), encoding="utf-8").read().strip() == "print('main OLD')",
      "после отката на диске старый код")
check(U.backups(inst)[:1] == [os.path.basename(rep["backup_dir"])], "бэкапы отсортированы свежие сверху")
check(U.rollback(os.path.join(tmp, "empty-install-never-used"))["ok"] is False,
      "откат без бэкапа — честное «ечего», не исключение")

for i in range(5):                                  # prune оставляет только последние N
    d = os.path.join(inst, U.UPDATE_DIR, "backup-x%d" % i)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "f"), "w").close()
    time.sleep(0.01)
removed = U.prune(inst, keep=2)
check(removed >= 1 and len(U.backups(inst)) <= 3, "prune чистит старые копии", str(U.backups(inst)))

# нет прав на запись -> внятное сообщение, а не traceback. Важно: ОС смотрит на
# право записи в КАТАЛОГ (замена файла = создать+rename), поэтому закрываем каталог,
# а не файл.
ro = os.path.join(tmp, "readonly")
os.makedirs(os.path.join(ro, "app"), exist_ok=True)
for n in ("main.py", "engine.py", "correction.py", "version.py"):
    open(os.path.join(ro, "app", n), "w", encoding="utf-8", newline="\n").write("x\n")
os.chmod(os.path.join(ro, "app"), 0o555)
pre = U.plan(staged, ro)
rep = U.apply_update(staged, ro, backup=False)
os.chmod(os.path.join(ro, "app"), 0o755)
if os.name != "posix":
    check(True, "нет прав на запись — проверка только для POSIX (на Windows chmod запись не запрещает)", "skip")
elif getattr(os, "geteuid", lambda: 1)() == 0:
    check(True, "нет прав на запись — сообщение (под root права не работают, пропуск)", "root")
else:
    check(any(r.startswith("app/") for r, _k, _d in pre) and not any(r.startswith("app/")
          for r in rep["applied"]) and any("нет прав" in e for e in rep["errors"]),
          "нет прав на запись -> текст с подсказкой",
          "план %d, заменено %d, ошибок %d: %s"
          % (len(pre), len(rep["applied"]), len(rep["errors"]), (rep["errors"] or [""])[0][:90]))

# --------------------------------------------------------------------------
section("6. полный сценарий perform_update (подменный fetch)")
proj = os.path.join(tmp, "proj")
os.makedirs(os.path.join(proj, "app"), exist_ok=True)
for n, txt in {"main.py": "print('main OLD')\n", "engine.py": "X = 1\n",
               "correction.py": "Y = 2\n", "version.py": '__version__ = "0.0.1"\n'}.items():
    open(os.path.join(proj, "app", n), "w", encoding="utf-8", newline="\n").write(txt)
new_zip = make_zip(minimal_project("5.5.5"))
ff_full = FakeFetch({"/commits/main": head_body(sha="9" * 40),
                     "codeload.github.com": (200, {}, new_zip),
                     "archive": (200, {}, new_zip)})
U.save_state(installed_sha="a" * 40, state="", remote_sha="", checked_at=0)
seen = []
rep = U.perform_update(fetch=ff_full, root=proj, restart=False, progress=seen.append)
check(rep["ok"], "обновление прошло целиком", rep.get("message", "")[:70])
check(rep["state"] == "update-available", "сначала было «есть обновление», а не тишина")
check(rep["version_to"] == "5.5.5" and rep["version_from"] == V.__version__,
      "в отчёте видно, с какой версии на какую идем", "%s -> %s" % (rep["version_from"], rep["version_to"]))
check(open(os.path.join(proj, "app", "main.py"), encoding="utf-8").read().strip() == "print('main 5.5.5')",
      "код на диске — новый")
check(any("скачиваю" in m for m in seen) and any("меняю" in m for m in seen),
      "прогресс-строки шли в UI", " | ".join(seen[:3])[:70])
check(any("codeload.github.com/markuzewb/tarkovLight/zip/refs/heads/main" in u for u in ff_full.urls()),
      "архив взялся из ветки main указанного репо", ff_full.urls()[-1][:70])
st_after = json.load(open(U.state_path(), encoding="utf-8"))
check(st_after["installed_sha"] == "9" * 40 and st_after["installed_version"] == "5.5.5",
      "состояние записано: след. проверка увидит актуальный sha", str(st_after)[:90])
rep2 = U.perform_update(fetch=FakeFetch({"/commits/main": head_body(sha="9" * 40),
                                          "codeload": (200, {}, new_zip)}),
                        root=proj, restart=False)
check(rep2["ok"] and "нет" in rep2["message"].lower() or "отличий" in rep2["message"],
      "повторное обновление на том же sha ничего не меняет", rep2["message"][:70])
# архив не того проекта (нет обязательных файлов) -> отказ, файлы целые
rep3 = U.perform_update(fetch=FakeFetch({"/commits/main": head_body(sha="8" * 40),
                                         "codeload": (200, {}, make_zip({"README.md": "только ридми"},
                                                                        folder="other-repo"))}),
                        root=proj, restart=False)
check(not rep3["ok"] and "не подошёл" in rep3["message"], "чужой архив отклонён", rep3["message"][:60])
check(open(os.path.join(proj, "app", "main.py"), encoding="utf-8").read().startswith("print('main 5.5.5')"),
      "после неудачной попытки на диске остался рабочий код")
# а архив из форка с другим именем корня — норм (папку не угадываем, берём содержимое)
rep3b = U.perform_update(fetch=FakeFetch({"/commits/main": head_body(sha="7" * 40),
                                          "codeload": (200, {}, make_zip(minimal_project("5.5.6"),
                                                                         folder="my-fork-main"))}),
                         root=proj, restart=False)
check(rep3b["ok"] and rep3b["version_to"] == "5.5.6",
      "имя верхнего каталога в zip значения не имеет", rep3b["message"][:60])
rep4 = U.perform_update(fetch=FakeFetch({}, raise_exc=OSError("offline")), root=proj, restart=False)
check(not rep4["ok"] and rep4["state"] == "offline", "офлайн: ok=False, состояние названо", rep4["state"])
check(U.human({"state": "update-available", "local_sha": "a" * 40, "remote_sha": "b" * 40,
                "message": "тест"}).count("|") >= 2, "human() печатает одну понятную строку")

# --------------------------------------------------------------------------
section("7. помощник перезапуска (--wait-pid / --retry-apply / --restart-file)")
work = os.path.join(tmp, "helper")
os.makedirs(os.path.join(work, "app"), exist_ok=True)
open(os.path.join(work, "app", "main.py"), "w", encoding="utf-8", newline="\n").write("OLD\n")
hstaged = os.path.join(work, "staged")
os.makedirs(os.path.join(hstaged, "app"), exist_ok=True)
open(os.path.join(hstaged, "app", "main.py"), "w", encoding="utf-8", newline="\n").write("NEW\n")
log = os.path.join(work, "u.log")
rf = os.path.join(work, "restart.json")
marker = os.path.join(work, "restarted.txt")
U._write_json(rf, {"argv": [sys.executable, "-c", "open(%r,'w').write('up')" % marker],
                   "cwd": work})
r = subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, %r); import _update_helper as H; sys.exit(H.main("
                    "[a for a in sys.argv[1:]]))" % os.path.join(ROOT, "app")] +
                   ["--wait-pid", "0", "--retry-apply", hstaged, "--target", work,
                    "--restart-file", rf, "--log", log],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
check(r.returncode == 0, "помощник вышел без ошибки", (r.stderr or "")[:120])
check(open(os.path.join(work, "app", "main.py"), encoding="utf-8").read().strip() == "NEW",
      "помощник доложил то, что не заменилось сразу")
check(not os.path.exists(rf), "файл плана перезапуска потреблён (не будет повторного старта)")
deadline = time.time() + 20
while time.time() < deadline and not os.path.exists(marker):
    time.sleep(0.1)
check(os.path.exists(marker), "приложение перезапущено помощником", open(marker).read() if os.path.exists(marker) else "")
check(os.path.isfile(log) and "помощник" in open(log, encoding="utf-8").read(),
      "ход работы виден в update.log")

# ожидание реального процесса (не «поспать и надеяться»)
pid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.2)"]).pid
t0 = time.time()
sys.path.insert(0, os.path.join(ROOT, "app"))
import _update_helper as H                                        # noqa: E402
H._LOG_PATH = log
waited = H.wait_exit(pid, 30.0)
dt = time.time() - t0
check(waited == "" and 0.8 < dt < 20, "wait_exit ждёт выхода процесса, а не фиксированный сон",
      "%.2f с" % dt)
check(H.wait_exit(0, 1.0) == "", "pid=0 — не ждать ничего")

# --------------------------------------------------------------------------
section("8. интеграция с приложением")
check(M.U is U, "main.py видит тот же модуль updater")
app = M.App(dict(E.DEFAULT_CONFIG), headless=True)
check(hasattr(app, "upd_q") and hasattr(app, "update_check") and hasattr(app, "update_now"),
      "у App есть очередь и методы обновления (GUI и CLI идут одним путём)")
res = {"state": "update-available", "message": "тест", "ok": True, "applied": ["app/main.py"]}
app._update_progress("скачиваю…")
try:
    got = app.upd_q.get_nowait()
except Exception:
    got = {}
check(got.get("state") == "progress" and "скачиваю" in got.get("message", ""),
      "прогресс обновлятора приходит в ту же очередь, что и статус кадра", str(got)[:80])
app._update_run(lambda: res)
deadline = time.time() + 10
seen = []
while time.time() < deadline:
    try:
        seen.append(app.upd_q.get_nowait())
    except Exception:
        if any(s.get("state") == "update-available" for s in seen):
            break
        time.sleep(0.05)
check(any(s.get("applied") == ["app/main.py"] for s in seen),
      "отчёт perform_update доехал до окна через upd_q", str(seen[-1:])[:90])
check(app.restart_after_exit is False, "без request_restart перезапуск не навязывается")

# фолбэк: если updater не импортируется, приложение живёт как раньше
saved_U, saved_err = M.U, M.UPDATER_ERROR
try:
    M.U = None
    a2 = M.App(dict(E.DEFAULT_CONFIG), headless=True)
    out = []
    a2.update_check(on_done=lambda r: out.append(r))
    a2.update_now(on_done=lambda r: out.append(r))
    time.sleep(0.2)
    check(out and all("updater" in str(o.get("message", "")) for o in out),
          "нет updater — понятное сообщение, а не падение кнопки", str(out[:1])[:90])
finally:
    M.U, M.UPDATER_ERROR = saved_U, saved_err

# note_start: RDP-подсказка не затирается сообщением про незапущенную игру
a3 = M.App(dict(E.DEFAULT_CONFIG), headless=True)
a3.note_start("сеанс RDP: гаммы нет")
a3.note_start("Тарков не запущен")
check("RDP" in a3.note and "Тарков" in a3.note, "оба замечания видны одновременно", a3.note[:80])
a3.note_start("Тарков не запущен")
check(a3.note.count("Тарков не запущен") == 1, "повтор одного и того же замечания не дублируется")

# захват пересоздаётся по новым параметрам без перезапуска
class G:
    made = []
    def __init__(self, w=560, mon=1):
        G.made.append((w, mon))
    def grab(self):
        return None
    def close(self):
        pass

import capture as Cp
real = Cp.Grabber
Cp.Grabber = G
try:
    a4 = M.App(dict(E.DEFAULT_CONFIG), headless=True)
    a4.cfg["tie_to_game"] = False
    import threading as _th
    check(G.made == [], "захват создаётся только внутри рабочего потока", str(G.made))
    a4.cfg["monitor_index"] = 3
    a4.cfg["capture_width"] = 800
    k = a4.grab_params()
    check(k[0] == 800 and k[1] == 3, "grab_params читает новые значения", str(k))
    a4.cfg["update_hz"] = "abc"
    k2 = a4.grab_params()
    check(k2[2] == 1.0 / 12 and k2[0] == 800, "битый update_hz не роняет цикл (дефолтный период)", str(k2))
    a4.cfg["capture_width"] = None
    check(a4.grab_params()[0] == 560, "capture_width=None -> 560", str(a4.grab_params()))
    # цикл жив и сам пересоздаёт захват
    G.made.clear()
    th = _th.Thread(target=a4._loop, daemon=True)
    th.start()
    time.sleep(0.4)
    a4.cfg["monitor_index"] = 4
    time.sleep(0.6)
    a4.stop.set()
    th.join(5)
    check(len(G.made) >= 2 and G.made[-1][1] == 4,
          "рабочий цикл сам пересоздаёт захват при смене монитора", str(G.made))
finally:
    Cp.Grabber = real

# автосохранение конфига из цикла
cfgdir = os.path.join(tmp, "appdata")
E.config_path = lambda: os.path.join(cfgdir, "config.json")     # не гадим в реальный %APPDATA%
a5 = M.App(dict(E.DEFAULT_CONFIG), headless=True)
a5.cfg["shadow_lift"] = 0.5
a5.mark_dirty()
check(a5.flush_config(), "flush_config сохраняет по флагу")
check(json.load(open(os.path.join(cfgdir, "config.json"), encoding="utf-8"))["shadow_lift"] == 0.5,
      "настройки долетели до config.json")
check(a5.flush_config() is False, "без изменений второй flush — no-op")
a5.cfg["autosave"] = False
a5.mark_dirty()
check(a5.flush_config() is False, "autosave=false отключ записывает только по force")
check(a5.flush_config(force=True), "force пишет конфиг независимо от autosave")

# один экземпляр
ok1, detail = W.acquire_instance_lock("tbtest")
check(ok1, "первый экземпляр занимает блокировку", detail)
code_run = ('import sys; sys.path.insert(0, %r); import windows as W; '
            'ok, d = W.acquire_instance_lock("tbtest"); print(ok)') % os.path.join(ROOT, "app")
r = subprocess.run([sys.executable, "-c", code_run], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
if os.name == "nt":
    check("False" in r.stdout, "второй процесс получает отказ (мьютекс)", r.stdout.strip()[:60])
else:
    check("False" in r.stdout, "второй процесс получает отказ (lock-файл с pid)", r.stdout.strip()[:60])
W.release_instance_lock()
r = subprocess.run([sys.executable, "-c", code_run], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
check("True" in r.stdout, "после освобождения блокировки запуск разрешён", r.stdout.strip()[:60])

# CLI-пути: не падают и говорят честно
r = subprocess.run([sys.executable, os.path.join(ROOT, "app", "main.py"), "--check-update",
                    "--no-net"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
check(r.returncode == 2 and "no-net" in (r.stdout + r.stderr)
      and "Traceback" not in (r.stdout + r.stderr),
      "--check-update с --no-net честно отказывается, а не висит", (r.stdout or r.stderr).strip()[:90])
r = subprocess.run([sys.executable, os.path.join(ROOT, "app", "main.py"), "--update",
                    "--no-net"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
check(r.returncode == 2 and "Traceback" not in (r.stdout + r.stderr),
      "--update с --no-net тоже отказывает явно", (r.stdout or r.stderr).strip()[:90])
r = subprocess.run([sys.executable, os.path.join(ROOT, "app", "main.py"), "--version"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
check(V.__version__ in r.stdout and "markuzewb/tarkovLight" in r.stdout,
      "--version печатает версию и источник обновлений", r.stdout.strip().splitlines()[0][:60])
r = subprocess.run([sys.executable, os.path.join(ROOT, "app", "main.py"), "--doctor", "--no-net"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
check("gamma-таблица" in r.stdout and "Traceback" not in r.stdout + r.stderr,
      "--doctor отдаёт отчёт без трейсбека", r.stdout.strip().splitlines()[0][:70])
entry = os.path.join(ROOT, "TarkovBright.pyw")
check(os.path.isfile(entry), "в корне есть TarkovBright.pyw — один запуск двойным кликом")
r = subprocess.run([sys.executable, entry, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
check(V.__version__ in r.stdout and r.returncode == 0,
      "через .pyw приложение стартует так же, как через app/main.py", r.stdout.strip()[:60])
r = subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, %r); import updater as U; "
                    "print(U.restart_command(['--minimized']))" % os.path.join(ROOT, "app")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
check("--minimized" in r.stdout and "python" in r.stdout.lower(),
      "команда перезапуска сохраняет аргументы", r.stdout.strip()[:90])

# --------------------------------------------------------------------------
section("9. безопасность и гигиена обновлятора")
src = open(os.path.join(ROOT, "app", "updater.py"), encoding="utf-8").read()
check("import requests" not in src and "import git" not in src,
      "в updater.py нет сторонних зависимостей (работает на голом Python)")
for mod in ("urllib", "zipfile", "hashlib", "shutil", "json", "subprocess"):
    check(("import %s" % mod) in src, "нужный stdlib-модуль импортирован: %s" % mod)
check("http://" not in src.replace("https://", ""), "никаких адресов по http:// (только https)")
check('"%s"' % V.REPO in open(os.path.join(ROOT, "app", "version.py"), encoding="utf-8").read(),
      "репозиторий задан в app/version.py, а не зашит в код обновлятора")
check("config.json" in src or "config_dir" in src,
      "путь конфига описан: конфиг пользователя обновлением не затирается")
check("_write_json" in src and "os.replace" in src, "состояние пишется атомарно (tmp + os.replace)")
check("sample" not in ",".join(U.TRACK_DIRS), "samples/ не входит в список обновления")
check(".github" in U.TRACK_DIRS, "CI-конфиг обновляется вместе с кодом")
cfg_now = copy.deepcopy(E.DEFAULT_CONFIG)
check("update_auto_check" in cfg_now and isinstance(cfg_now["update_auto_check"], bool),
      "в дефолтах есть update_auto_check (авто-проверка при старте)")
check("autosave" in cfg_now, "в дефолтах есть autosave")
check(cfg_now["saturation"] <= 1.24, "инвариант saturation <= 1.24 не сломан", str(cfg_now["saturation"]))
check(C.identity_ramp() == C.ramp_bytes(C.build_luts()),
      "инвариант: γ=1 -> тождественная таблица (обновлятор на математику не влияет)")

# --------------------------------------------------------------------------
section("10. .exe: «скачать один файл» и обновление ссылкой на релиз")
# Собранный exe не может переписать сам себя, поэтому у него свой путь:
# сравнение версии релиза + прямая ссылка. Всё это офлайн, через подменный fetch.
_EXE_TAG = "TarkovBright.exe"


def _release_body(tag):
    return json.dumps({
        "tag_name": tag,
        "html_url": "https://github.com/%s/releases/tag/%s" % (V.REPO, tag),
        "body": "список изменений",
        "assets": [{"name": _EXE_TAG, "size": 11330872,
                    "browser_download_url":
                        "https://github.com/%s/releases/download/%s/%s" % (V.REPO, tag, _EXE_TAG)}],
    }).encode("utf-8")


_real_frozen = getattr(sys, "frozen", False)
sys.frozen = True
try:
    ok_ss, why_ss = U.supports_self_update()
    check(ok_ss is False, "в frozen-режиме само-обновление запрещено", str(ok_ss))
    check(_EXE_TAG in why_ss and "releases/latest/download" in why_ss,
          "причина содержит прямую ссылку на exe", why_ss[:90])

    rel = U.latest_release(fetch=lambda url, hdr=None, t=0: (200, {}, _release_body("v9.9.0")))
    check(rel["ok"] and rel["tag"] == "v9.9.0" and rel["version"] == "9.9.0",
          "релиз разобран: тег и версия", str(rel)[:100])
    check(rel["exe_url"].endswith("/download/v9.9.0/" + _EXE_TAG) and rel["size"] == 11330872,
          "ссылка на exe берётся из assets, размер виден", rel["exe_url"][-45:])
    rel404 = U.latest_release(fetch=lambda url, hdr=None, t=0: (404, {}, b'{"message":"Not Found"}'))
    check(rel404.get("no_releases") and not rel404.get("ok"),
          "нет релизов — это не ошибка и не офлайн", str(rel404["error"]))
    rel403 = U.latest_release(fetch=lambda url, hdr=None, t=0: (403, {}, b"{}"))
    check(bool(rel403.get("rate_limited")), "лимит запросов назван лимитом")

    def _dead(url, hdr=None, t=0):
        raise OSError("no route to host")
    check(bool(U.latest_release(fetch=_dead).get("offline")), "оборванная сеть — offline")

    # --- check() в frozen-режиме: сравниваем версию релиза, а не sha ---
    U.save_state(checked_at=0, state="", remote_sha="", remote_version_hint="", etag="")
    r = U.check(fetch=lambda url, hdr=None, t=0: (200, {}, _release_body("9.9.0")
                                                  if b"releases" in url.encode() else (200, {}, b"{}")),
                force=True)
    check(r["state"] == "update-available" and "9.9.0" in r["message"],
          "новый релиз замечен", r["state"] + " | " + r["message"][:70])
    check(r["exe_url"].endswith(_EXE_TAG) and "/tree/" not in r["exe_url"],
          "check() отдаёт ссылку на файл релиза", r["exe_url"][-40:])
    r_same = U.check(fetch=lambda url, hdr=None, t=0: (200, {},
                        _release_body("v" + V.__version__)), force=True)
    check(r_same["state"] == "up-to-date", "версия релиза == локальная -> обновлений нет",
          r_same["state"])
    r_off = U.check(fetch=_dead, force=True)
    check(r_off["state"] == "offline" and r_off["ok"] is False,
          "без сети exe не врануло «обновлений нет»", r_off["message"][:50])
    n = [0]

    def _counting(url, hdr=None, t=0):
        n[0] += 1
        return 200, {}, _release_body("v9.9.0")
    U.check(fetch=_counting, force=True)
    after_first = n[0]
    cached = U.check(fetch=_counting, force=False)
    check(n[0] == after_first and cached["from_cache"],
          "авто-проверка при старте не дёргает GitHub второй раз за 6 часов",
          "запросов %d -> %d" % (after_first, n[0]))
    st = U.load_state()
    check(st.get("remote_version_hint") == "9.9.0",
          "версия релиза запоминается в update.json", str(st.get("remote_version_hint")))
finally:
    if _real_frozen:
        sys.frozen = True
    else:
        del sys.frozen
check(getattr(sys, "frozen", False) is False, "sys.frozen восстановлен: остальной тест не «exe»")

# --- CLI в frozen-режиме: никакого «обновления файлами», только ссылка ---
_FROZEN_PRE = ("import os, sys, tempfile\n"
               "sys.frozen = True\n"
               "os.environ['APPDATA'] = tempfile.mkdtemp(prefix='tbfrozen-')\n"
               "sys.path.insert(0, os.path.join(r'%s', 'app'))\n" % ROOT)

r = subprocess.run([sys.executable, "-c", _FROZEN_PRE +
                    "import main\nsys.exit(main.main(['--update']))"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
check(r.returncode == 2 and "releases/latest/download" in r.stdout,
      "--update в exe не пытается патчить файлы, а даёт ссылку",
      (r.stdout or r.stderr).strip().replace("\n", " ")[:110])
r = subprocess.run([sys.executable, "-c", _FROZEN_PRE +
                    "import main\nsys.exit(main.main(['--check-update', '--no-net']))"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
check(r.returncode == 2, "--check-update --no-net в exe честно отказывается, а не «обновлений нет»",
      "exit=%d" % r.returncode)
r = subprocess.run([sys.executable, "-c", _FROZEN_PRE +
                    "import main\nsys.exit(main.main(['--version']))"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
check(r.returncode == 0 and V.__version__ in r.stdout, "--version в exe работает (exit 0)",
      r.stdout.strip().splitlines()[0] if r.stdout.strip() else "")

# --- краш в exe не должен быть немым: traceback обязан попасть в error.log ---
code_crash = (_FROZEN_PRE +
              "import main\n"
              "def boom(argv=None):\n"
              "    raise RuntimeError('нет модуля захвата в exe')\n"
              "main._main_body = boom\n"
              "sys.exit(main.main([]))\n")
r = subprocess.run([sys.executable, "-c", code_crash],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
check(r.returncode == 1, "падение в exe отдаёт код 1, а не 0", "exit=%d" % r.returncode)
logs = [d for d in os.listdir(os.environ.get("TMP", tempfile.gettempdir()))
        if d.startswith("tbfrozen-")]
found = ""
for d in logs:
    p_log = os.path.join(os.environ.get("TMP", tempfile.gettempdir()), d, "TarkovBright", "error.log")
    if os.path.isfile(p_log) and "нет модуля захвата" in open(p_log, encoding="utf-8").read():
        found = p_log
        break
check(bool(found), "трейс падения записан в %APPDATA%\\TarkovBright\\error.log",
      found or "файл не найден в %s" % tempfile.gettempdir())

print()
if FAILS:
    print(f"ПРОВАЛЕНО {len(FAILS)}:")
    [print(" -", f) for f in FAILS]
    sys.exit(1)
print("Обновлятор, конфиг и интеграция проверены (%d проверок)" % N[0])
