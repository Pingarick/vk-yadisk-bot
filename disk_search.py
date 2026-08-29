# -*- coding: utf-8 -*-
"""
disk_search.py — поиск файлов на Яндекс Диске по названию + дедупликация.

Работлет через REST API cloud-api.yandex.net:
  GET https://cloud-api.yandex.net/v1/disk/resources
  `Authorization: OAuth <token>`

Возвращает список словарей-файлов. Зависит ТОЛЬКО от stdlib (urllib),
fuzzy-поиск через difflib — также stdlib. Внешних зависимостей нет.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

API_BASE = "https://cloud-api.yandex.net/v1/disk/resources"


@dataclass
class DiskFile:
    """Одна найденная запись на диске."""

    name: str
    path: str
    href: str            # прямая ссылка на скачивание
    mime_type: str = ""
    size: int = 0
    is_dir: bool = False
    # нормализованный ключ для дедупликации по имени
    _norm: str = field(default="", repr=False)

    def norm_name(self) -> str:
        if not self._norm:
            self._norm = normalize_name(self.name)
        return self._norm


def normalize_name(name: str) -> str:
    """
    Нормализация имени для сравнения/дедупа:
      - lower, срезать пробелы
      - снять суффикс-дубль от системы " (1)", "-1", "_1"
      - схлопнуть повторные пробелы
    """
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    # "file (1).txt" -> "file.txt" ; "file-1" -> "file" (но не трогаем цифры внутри слова)
    s = re.sub(r"\s+\(\d+\)(?=\.[a-z0-9]+$)", "", s)
    s = re.sub(r"[-_\s]+(\d+)(?=\.[a-z0-9]+$)", r"\1", s)  # fallback: file_1.txt->file.txt
    s = re.sub(r"[-_]\s*(\d+)$", "", s)
    # схлопнуть дубль-пробелы (в т.ч. перед расширением) и срезать крайние
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+(\.[a-z0-9]+)$", r"\1", s)  # "мой файл .pdf" -> "мой файл.pdf"
    return s


def walk_all(token: str, path: str = "/", limit: int = 1000, depth: int = 0) -> list[DiskFile]:
    """
    Рекурсивный обход папок диска. Возвращает плоский список файлов.
    depth — защита от зацикливания (глубина вложенности папок).
    """
    if depth > 20:
        return []
    out: list[DiskFile] = []
    # список содержимого текущей папки
    items = _list_folder(token, path, limit)
    for it in items["items"]:
        if it.get("type") == "dir":
            # спуститься в папку
            out.extend(walk_all(token, it["path"], limit, depth + 1))
        else:
            out.append(_to_diskfile(it))
    return out


def _list_folder(token: str, path: str, limit: int) -> dict:
    qs = urllib.parse.urlencode({
        "path": path,
        "limit": str(limit),
        "media_type": "file",  # только файлы в ответе по умолчанию у пар так много
    })
    url = f"{API_BASE}?{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": "OAuth " + token,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)["_embedded"]
            _has_more = data.get("has_more", False)
            return data  # items внутри
    except urllib.error.HTTPError as e:
        # пробросить понятную ошибку наружу
        raise DiskApiError(f"HTTP {e.code} на {path}: {e.read().decode(err='ignore')[:200]}")
    except urllib.error.URLError as e:
        raise DiskApiError(f"Сеть не отвечает: {e.reason}")


def _to_diskfile(it: dict) -> DiskFile:
    return DiskFile(
        name=it.get("name", ""),
        path=it.get("path", ""),
        href=it.get("file", "") or it.get("download", ""),  # href скачивания
        mime_type=it.get("mime_type", ""),
        size=it.get("size", 0),
        is_dir=False,
    )


def fetch_meta(token: str, path: str = "/") -> dict:
    """Собрать весь список файлов по всему диску (или из VK_YW_DISK_PATH)."""
    qs = urllib.parse.urlencode({"path": path})
    url = f"{API_BASE}?{qs}&limit=1000"
    req = urllib.request.Request(url, headers={
        "Authorization": "OAuth " + token,
        "Accept": "application/json",
    })
    try:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode(err="ignore")
            if e.code == 401:
                raise DiskApiError("Токен Яндекс Диска недействителен (401).")
            raise DiskApiError(f"HTTP {e.code}: {body[:200]}")
    except urllib.error.URLError as e:
        raise DiskApiError(f"Сеть: {e.reason}")


def index_files(token: str, base_path: str) -> list[DiskFile]:
    """Обойти весь диск и вернуть плоский список файлов (готовый к поиску)."""
    files = walk_all(token, base_path or "/")
    # дедупликация по нормализованному имени ещё до поиска
    return dedupe(files)


def dedupe(files: list[DiskFile],
            newest_first: bool = False) -> list[DiskFile]:
    """
    Убрать дубли по названию (нормализованному имени).
    Возвращает список с УНИКАЛЬНЫМИ именами.
    """
    seen: set[str] = set()
    result: list[DiskFile] = []
    order = files if not newest_first else sorted(
        files, key=lambda f: f.path, reverse=True  # placeholder сортировки
    )
    for f in order:
        key = f.norm_name()
        if key and key not in seen:
            seen.add(key)
            result.append(f)
    return result


def search(token: str,
           query: str,
           base_path: str = "/",
           mode: str = "fuzzy",
           threshold: float = 0.6,
           limit: int = 10) -> list[DiskFile]:
    """
    Поиск по названию.
    mode="exact": совпадение с нормализованным именем или его началом.
    mode="fuzzy": подстрока-совпадение + difflib-похожесть >= threshold.
    Возвращает первые `limit` уникальных по имени файлов.
    """
    files = index_files(token, base_path)
    q = normalize_name(query)
    if not q:
        return []
    hits: list[DiskFile] = []
    for f in files:
        n = f.norm_name()
        if q in n:
            hits.append(f)
            continue
        if mode == "fuzzy":
            ratio = difflib.SequenceMatcher(None, q, n).ratio()
            if ratio >= threshold:
                hits.append(f)
    # отсортировать по похожести (чем ближе, тем выше)
    hits.sort(key=lambda f: difflib.SequenceMatcher(None, q, f.norm_name()).ratio(),
              reverse=True)
    return hits[:limit]


class DiskApiError(Exception):
    """Ошибка обращения к API Яндекс Диска — выводится в боте."""


# ─────────────────────────────────────────────────────────────────
# CLI-заглушка для быстрой проверки локально:  python disk_search.py
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    tk = os.environ.get("YANDEX_DISK_TOKEN", "")
    if not tk or not sys.stdin.isatty():
        print("Укажи YANDEX_DISK_TOKEN и передай запрос аргументом.")
        print("  пример: YANDEX_DISK_TOKEN=... python disk_search.py 'договор'")
        sys.exit(0)
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        res = search(tk, query, base_path=os.environ.get("VK_YW_DISK_PATH", "/"))
    except DiskApiError as e:
        print(f"[ошибка] {e}")
        sys.exit(1)
    if not res:
        print("Ничего не найдено.")
    else:
        for f in res:
            print(f"[{f.size:>10} B] {f.name}  →  {f.href}")
