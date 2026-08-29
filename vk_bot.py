# -*- coding: utf-8 -*-
"""
vk_bot.py — приём сообщений через VK Bots Long Poll + отправка ответов.

Внешняя зависимость: requests.
API ВК: https://vk.com/dev/botslongpoll / messages.getLongPollServer
"""
from __future__ import annotations

import json
import logging
import os
import time

import requests

from disk_search import DiskApiError, search

log = logging.getLogger("vk_bot")

VK_API = "https://api.vk.com/method"
API_VERSION = "5.199"


class VkBotError(Exception):
    pass


class DotEnv:
    """Мини-чит .env без внешних пакетов (поддержка кавычек и комментариев)."""

    def __init__(self, path: str = ".env"):
        self.data: dict[str, str] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    self.data[k.strip()] = v.strip().strip('"').strip("'")


class VkClient:
    """Обёртка над VK API + long poll."""

    def __init__(self, token: str, group_id: str):
        self.token = token
        self.group_id = str(group_id)

    def _call(self, method: str, params: dict) -> dict:
        # подписка с access_token и group_id для групповых боётов
        params.update({
            "access_token": self.token,
            "v": API_VERSION,
            "group_id": self.group_id,
        })
        r = requests.post(f"{VK_API}/{method}", data=params, timeout=30)
        body = r.json()
        if "error" in body:
            err = body["error"]
            raise VkBotError(f"{method}: [{err.get('error_code')}] {err.get('error_msg')}")
        return body.get("response", {})

    def get_long_poll(self) -> tuple[str, str, str]:
        """Вернуть (server, key, ts)."""
        resp = self._call("messages.getLongPollServer", {"need_pts": 0})
        return resp["server"], resp["key"], resp["ts"]

    # отправка ссылки-ответа (вариант «просто ссылкой»)
    def send_text(self, peer_id: int, message: str) -> None:
        self._call("messages.send", {"peer_id": peer_id, "message": message})

    # отправка ВЛОЖЕНИЯ (как файл) — заполнить при «файлом» (см. upload_doc)
    def send_doc(self, peer_id: int, title: str, file_url: str) -> None:
        """
        Если клиент хочет «файлом» — сюда вставляется аплоад в VK:
          1. скачать file_url с Диска (GET с Authorization) в temp-файл
          2. docs.getMessagesUploadServer → upload → vk_api.docs.save
          3. messages.send(attachment=doc{owner_id}_{id})
        Здесь скелет: пишем падает печатно, а реальный аплоад — TODO по ТЗ.
        """
        # TODO(по ТЗ): реальная подгрузка вложения. Пока отвечаем ссылкой.
        log.warning("send_doc: upload в VK ещё не реализован — отвечаю ссылкой.")
        self.send_text(peer_id, f"📎 {title}\nСкачать: {file_url}")

    def success(self, peer_id: int, files) -> None:
        if not files:
            self.send_text(peer_id, "Не нашёл файлов по этому запросу.")
            return
        lines = []
        for f in files:
            lines.append(f"📄 {f.name}\n🔗 {f.href}")
        # лимит на одно сообщение и объём
        message = "\n\n".join(lines)
        # простая эвристика: если длинно — режем и шлём по частям
        for part in _split_message(message, max_len=4000):
            self.send_text(peer_id, part)

    def error(self, peer_id: int, text: str) -> None:
        self.send_text(peer_id, f"⚠️ {text}")


def _split_message(msg: str, max_len: int = 4000) -> list[str]:
    """Разбить длинный ответ на куски (учитывая размер вентиля)."""
    out = []
    cur = ""
    for line in msg.split("\n"):
        if len(cur) + len(line) + 1 > max_len:
            out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out or [""]


def handle_message(bot: VkClient, peer_id: int, text: str, env: dict) -> None:
    """Осн. хендлер: полный запрос → поиск → ответ."""
    query = text.strip().lower()
    if not query or query in {"/start", "/help", "hello", "привет", "начать"}:
        bot.send_text(peer_id,
                      "Отправь название файла — найду на Яндекс Диске.\n"
                      "Пример: договор аренды")
        return
    log.info("Ищу: %r (client %s)", query, peer_id)
    try:
        files = search(
            token=env["YANDEX_DISK_TOKEN"],
            query=query,
            base_path=env.get("VK_YW_DISK_PATH", "/"),
            mode=env.get("VK_YW_SEARCH_MODE", "fuzzy"),
            threshold=float(env.get("VK_YW_FUZZY_THRESHOLD", 0.6)),
            limit=int(env.get("VK_YW_MAX_RESULTS", 10)),
        )
    except DiskApiError as e:
        bot.error(peer_id, f"Ошибка диска: {e}")
        return
    bot.success(peer_id, files)


def listen_and_serve(vk: VkClient, env: dict) -> None:
    """Главный цикл long poll."""
    while True:
        try:
            server, key, ts = vk.get_long_poll()
        except VkBotError as e:
            log.error("LongPool server error: %s", e)
            time.sleep(3)
            continue
        log.info("Слушаю Long Poll... (server=%s)", server)
        while True:
            payload = {"act": "a_check", "key": key, "ts": ts, "wait": 25}
            try:
                r = requests.post(f"https://{server}", data=payload, timeout=35)
                data = r.json()
                ts = data["ts"]
                for ev in data.get("updates", []):
                    if ev[0] == 4:  # новое сообщение
                        # ev: [type, message_id, flags, peer_id, timestamp, body, ...]
                        peer_id = ev[3]
                        body = ev[5]
                        handle_message(vk, peer_id, str(body), env)
            except Exception as e:
                log.warning("LongPoll iteration error: %s", e)
                time.sleep(2)
                break  # переподключиться к серверу


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    env = DotEnv().data
    token = env.get("VK_ACCESS_TOKEN", "")
    gid = env.get("VK_GROUP_ID", "")
    if not token or not gid:
        raise SystemExit(
            "Заполни VK_ACCESS_TOKEN и VK_GROUP_ID в .env (см. README)."
        )
    vk = VkClient(token, gid)
    listen_and_serve(vk, env)


if __name__ == "__main__":
    main()
