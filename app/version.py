"""Версия и координаты источника обновлений.

Один файл на всё приложение: его читает `--version`, окно (подпись «v…») и
`updater.py` (сверяет локальную версию с той, что лежит в архиве на GitHub).

Формат строки версии важен: `updater._parse_version` разбирает именно
`__version__ = "X.Y.Z"`, поэтому правьте аккуратно — по нему приложение
решает, нужно ли вообще что-то скачивать.
"""
from __future__ import annotations

APP_NAME = "TarkovBright"
WINDOW_TITLE = "Tarkov Bright — авто-гамма"

# мажор.минор.патч: патч = правки «на месте», минор = новые фичи/UI
__version__ = "1.3.2"

# Откуда брать обновления. Публичный репозиторий -> токен не нужен (лимит
# GitHub API для анонимных запросов 60/час на IP, и это учитывает кэш ETag).
REPO = "markuzewb/tarkovLight"
BRANCH = "main"

# Сколько часов не трогать GitHub при авто-проверке при старте
# (кнопка «Проверить» работает сразу, лимит не трогая).
CHECK_INTERVAL_H = 6


def version_tuple(text: str | None = None) -> tuple:
    """'1.10.2' -> (1, 10, 2). Неполные/битые строки превращает в корректный
    кортеж из трёх чисел, чтобы сравнение никогда не бросало исключение."""
    parts = []
    for chunk in (text or __version__).strip().lstrip("vV").split(".")[:3]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(remote: str | None, local: str | None = None) -> bool:
    """Строгое сравнение версий (1.10.0 новее, чем 1.9.9)."""
    return version_tuple(remote) > version_tuple(local or __version__)
