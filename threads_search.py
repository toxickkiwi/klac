"""Поиск в Threads постов, где ищут специалиста.

Посты собирает парсер на Apify (настройки — config_threads.yaml, ключ — APIFY_TOKEN).
Подходящие посты: есть слова «ищу/ищем/нужен…», есть профессия из config.yaml
и есть признак Беларуси («Минск», «РБ», «BYN»…). Уже присланные не повторяются.

Запуск: PROFILE=threads python parser.py   (или напрямую python threads_search.py)
"""

import html
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

import parser as vacancies

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config_threads.yaml"
SENT_FILE = ROOT / "sent_threads.json"
BUTTON = {"text": "🧵 Ещё из Threads", "data": "threads"}


def any_word(words, text):
    return any(re.search(vacancies.word_pattern(w), text) for w in words or [])


def fetch_posts(config, token):
    """Запускает парсер Apify и ждёт результат (до ~5 минут)."""
    actor = config["apify_actor"]
    resp = requests.post(
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
        params={"token": token, "timeout": 280},
        json={"searchKeywords": config["search"], "sort": "recent",
              "maxItems": config.get("max_posts", 40)},
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()


def post_time(post):
    """Время поста: у разных парсеров поле называется по-разному."""
    value = post.get("takenAt") or post.get("timestamp") or post.get("date")
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            value = float(value)
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, ZoneInfo("UTC"))
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None


def select_posts(posts, config, main_config, sent):
    """Оставляет посты, где ищут специалиста нужной сферы в Беларуси."""
    profession_words = [w for c in main_config["categories"] for w in c["title"]]
    oldest = datetime.now(ZoneInfo("UTC")) - timedelta(days=config.get("max_age_days", 3))
    chosen, seen = [], set(sent["ids"])
    for post in posts:
        text = (post.get("text") or post.get("caption") or "").strip()
        url = post.get("url") or post.get("permalink") or ""
        post_id = str(post.get("postId") or post.get("id") or post.get("code") or url)
        low = text.lower().replace("ё", "е")
        when = post_time(post)
        if (not text or not url or post_id in seen
                or (when is not None and when < oldest)
                or not any_word(config["hiring_words"], low)
                or not any_word(profession_words, low)
                or not any_word(config["belarus_words"], low)
                or any_word(config.get("stop_words"), low)):
            continue
        seen.add(post_id)
        chosen.append({
            "id": post_id, "url": url, "text": text, "when": when,
            "username": post.get("username") or post.get("ownerUsername") or "",
        })
    chosen.sort(key=lambda p: p["when"] or oldest, reverse=True)  # свежие первыми
    return chosen[: config.get("per_day", 10)]


def build_message(posts, extra):
    esc = html.escape
    parts = ["🧵 <b>Ищут специалистов в Threads</b>"]
    if not posts:
        parts.append("Новых подходящих постов не нашлось." if extra
                     else "Сегодня новых подходящих постов не нашлось.")
    for number, post in enumerate(posts, 1):
        author = f"@{post['username']}" if post["username"] else "автор"
        when = f" · {post['when'].astimezone(ZoneInfo('Europe/Minsk')):%d.%m}" if post["when"] else ""
        text = vacancies.shorten(re.sub(r"\s+", " ", post["text"]), 220)
        parts.append(f"{number}. <b>{esc(author)}</b>{when}\n{esc(text)}\n🔗 {esc(post['url'])}")
    return "\n\n".join(parts)


def main(dry_run=False):
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        print("APIFY_TOKEN не задан — поиск в Threads пропущен (см. README).")
        return
    extra = os.environ.get("MODE") == "extra"
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    main_config = yaml.safe_load(vacancies.CONFIG_FILE.read_text(encoding="utf-8"))
    count = os.environ.get("COUNT", "").strip()
    if count:
        config["per_day"] = int(count)
    sent = vacancies.load_sent(SENT_FILE)
    today = datetime.now(ZoneInfo("Europe/Minsk")).date().isoformat()
    if not extra and os.environ.get("SKIP_IF_SENT") == "1" and sent.get("last_daily") == today:
        print("Подборка из Threads сегодня уже отправлена — пропускаем.")
        return

    try:
        posts = fetch_posts(config, token)
    except requests.RequestException as err:
        print(f"Apify не ответил: {err}", file=sys.stderr)
        if not dry_run:
            vacancies.send_telegram("⚠️ Не получилось собрать посты из Threads: Apify не ответил. "
                                    "Проверьте APIFY_TOKEN и баланс на apify.com.", only_owner=True)
        return
    print(f"Постов от Apify: {len(posts)}")
    picked = select_posts(posts, config, main_config, sent)
    message = build_message(picked, extra)
    print("\n" + message + "\n")
    if dry_run:
        print("(--dry-run: ничего не отправлено и не сохранено)")
        return
    vacancies.send_telegram(message, with_button=True, button=BUTTON)
    for post in picked:
        post["key"] = post["id"]
    if not extra:
        sent["last_daily"] = today
    vacancies.save_sent(sent, picked, SENT_FILE)
    print("Отправлено в Telegram.")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
