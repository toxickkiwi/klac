"""Вакансии из Threads для канала «Клацай, працуй!».

Ищет свежие посты в Threads через официальный API (keyword_search) по фразам
из config_threads.yaml, оставляет посты-вакансии по темам из config.yaml
и присылает их в Telegram тем же ботом: автор, начало поста, ссылка.

Запуск:
    python threads_parser.py            — найти посты и отправить в Telegram
    python threads_parser.py --dry-run  — только показать сообщение, ничего не отправлять

Переменные окружения:
    THREADS_TOKEN                     — токен доступа Threads API (см. README)
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  — как у parser.py
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yaml

from parser import CONFIG_FILE, ROOT, find_category, send_telegram, shorten, word_pattern

THREADS_CONFIG_FILE = ROOT / "config_threads.yaml"
THREADS_SENT_FILE = ROOT / "sent_threads.json"
SENT_LIMIT = 3000  # сколько последних отправленных постов помнить
API = "https://graph.threads.net/v1.0/keyword_search"
FIELDS = "id,text,permalink,timestamp,username,is_reply"
TEXT_PREVIEW = 300  # сколько символов поста показывать в сообщении


class TokenError(Exception):
    """Токен Threads неверный или истёк — дальше искать бессмысленно."""


def search_threads(query, token, since):
    resp = requests.get(API, params={
        "q": query, "search_type": "RECENT", "fields": FIELDS,
        "since": int(since.timestamp()), "limit": 100, "access_token": token,
    }, timeout=30)
    if not resp.ok:
        error = resp.json().get("error", {}) if resp.headers.get("content-type", "").startswith(
            "application/json") else {}
        if error.get("code") == 190 or resp.status_code == 401:
            raise TokenError(error.get("message", resp.text))
        raise RuntimeError(f"{resp.status_code} {error.get('message', resp.text)}")
    return resp.json().get("data", [])


def has_any(words, text):
    return any(re.search(word_pattern(w), text) for w in words or [])


def post_ok(post, threads_config, config):
    """Пост — вакансия по нашей теме, из нужного места и без стоп-слов. Возвращает сферу или None."""
    text = (post.get("text") or "").lower()
    if not text or post.get("is_reply"):
        return None
    if has_any(threads_config.get("stop_words"), text):
        return None
    if threads_config.get("vacancy_words") and not has_any(threads_config["vacancy_words"], text):
        return None
    if threads_config.get("place_words") and not has_any(threads_config["place_words"], text):
        return None
    return find_category(text, config)


def same_post_key(post):
    """Один и тот же текст, выложенный несколько раз, присылается однажды."""
    return re.sub(r"[^\w]+", " ", (post.get("text") or "").lower())[:200].strip()


def collect_posts(threads_config, config, sent, token):
    since = datetime.now(ZoneInfo("UTC")) - timedelta(days=threads_config.get("days", 3))
    sent_ids, sent_keys = set(sent["ids"]), set(sent["keys"])
    found, problems, errors = {}, [], 0
    for query in threads_config["search"]:
        try:
            posts = search_threads(query, token, since)
        except TokenError as err:
            print(f"[threads] токен не подходит: {err}", file=sys.stderr)
            problems.append("⚠️ Threads не принимает токен — скорее всего, он истёк. "
                            "Получите новый и обновите секрет THREADS_TOKEN (см. README).")
            break
        except Exception as err:
            errors += 1
            print(f"[threads] ошибка при поиске «{query}»: {err}", file=sys.stderr)
            continue
        print(f"[threads] «{query}»: постов {len(posts)}")
        for post in posts:
            key = same_post_key(post)
            if post["id"] in sent_ids or key in sent_keys or key in found:
                continue
            category = post_ok(post, threads_config, config)
            if category:
                post.update(key=key, category=category["name"], priority=category["priority"])
                found[key] = post
        time.sleep(1)
    if errors == len(threads_config["search"]):
        problems.append("⚠️ Threads: ни один поиск не сработал — подробности в логе GitHub Actions.")
    # Сначала важные сферы, внутри сферы — самые свежие.
    posts = sorted(found.values(), key=lambda p: p.get("timestamp", ""), reverse=True)
    posts.sort(key=lambda p: p["priority"])
    return posts[:threads_config.get("per_day", 10)], problems


def build_message(posts, problems, today):
    esc = html.escape
    parts = [f"🧵 <b>Вакансии из Threads на {today:%d.%m}</b>"]
    if not posts:
        parts.append("Новых подходящих постов не нашлось.")
    for number, post in enumerate(posts, 1):
        when = ""
        if post.get("timestamp"):
            moment = datetime.strptime(post["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
            when = f", {moment.astimezone(ZoneInfo('Europe/Minsk')):%d.%m}"
        text = shorten(" ".join(post["text"].split()), TEXT_PREVIEW)
        parts.append("\n".join([
            f"{number}. <b>@{esc(post.get('username', ''))}</b>{when} · {esc(post['category'])}",
            esc(text),
            f"🔗 {esc(post['permalink'])}",
        ]))
    parts.extend(esc(p) for p in problems)
    return "\n\n".join(parts)


def load_sent():
    if THREADS_SENT_FILE.exists():
        return json.loads(THREADS_SENT_FILE.read_text(encoding="utf-8"))
    return {"ids": [], "keys": []}


def save_sent(sent, posts):
    sent["ids"] = (sent["ids"] + [p["id"] for p in posts])[-SENT_LIMIT:]
    sent["keys"] = (sent["keys"] + [p["key"] for p in posts])[-SENT_LIMIT:]
    THREADS_SENT_FILE.write_text(json.dumps(sent, ensure_ascii=False, indent=1) + "\n",
                                 encoding="utf-8")


def main():
    dry_run = "--dry-run" in sys.argv
    token = os.environ.get("THREADS_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан THREADS_TOKEN (см. README, раздел про Threads).")
    if not dry_run and not (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        raise SystemExit("Не заданы TELEGRAM_TOKEN и TELEGRAM_CHAT_ID (см. README).")

    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    threads_config = yaml.safe_load(THREADS_CONFIG_FILE.read_text(encoding="utf-8"))
    sent = load_sent()
    today = datetime.now(ZoneInfo("Europe/Minsk"))

    posts, problems = collect_posts(threads_config, config, sent, token)
    message = build_message(posts, problems, today)
    print("\n" + message + "\n")
    if dry_run:
        print("(--dry-run: ничего не отправлено и не сохранено)")
        return
    send_telegram(message, only_owner=config.get("send_to") == "owner")
    save_sent(sent, posts)
    print("Отправлено в Telegram.")


if __name__ == "__main__":
    main()
