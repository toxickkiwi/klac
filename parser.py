"""Парсер вакансий для канала «Клацай, працуй!».

Раз в день ищет вакансии на rabota.by и praca.by, выбирает новые по настройкам
из config.yaml и присылает их в Telegram: название, кратко что делать, ссылка.

Запуск:
    python parser.py            — найти вакансии и отправить в Telegram
    python parser.py --dry-run  — только показать сообщение, ничего не отправлять
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.yaml"
SENT_FILE = ROOT / "sent.json"
SENT_LIMIT = 3000  # сколько последних отправленных вакансий помнить

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
REQUEST_PAUSE = 1.5  # секунд между запросами, чтобы не нагружать сайты

DUTY_HEADING = re.compile(
    r"обязанност|задачи|чем (предстоит|нужно|будешь|будете)|что (нужно |предстоит )?делать"
    r"|функционал|responsibilit|what you.ll do",
    re.IGNORECASE,
)
BLOCK_TAGS = ["p", "li", "ul", "ol", "div", "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr"]
ORG_FORMS = re.compile(r"\b(ооо|оао|зао|одо|чуп|чтуп|уп|ип|ао|пао|сооо|иооо|ltd|llc)\b")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})


def fetch(url):
    """Скачивает страницу; при сбое пробует ещё раз."""
    for attempt in range(3):
        try:
            time.sleep(REQUEST_PAUSE)
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as err:
            last_error = err
            time.sleep(3 * (attempt + 1))
    raise last_error


# ---------- поиск на сайтах ----------

def search_rabota(query):
    url = (
        "https://rabota.by/search/vacancy?area=16&order_by=publication_time"
        f"&search_period=7&text={quote(query)}"
    )
    soup = BeautifulSoup(fetch(url), "html.parser")
    found = []
    for card in soup.select('[data-qa="vacancy-serp__vacancy"]'):
        link = card.select_one('[data-qa="serp-item__title"]')
        match = link and re.search(r"/vacancy/(\d+)", link.get("href", ""))
        if not match:
            continue
        company = card.select_one('[data-qa="vacancy-serp__vacancy-employer-text"]')
        address = card.select_one('[data-qa="vacancy-serp__vacancy-address"]')
        found.append({
            "source": "rabota.by",
            "id": "rabota:" + match.group(1),
            "url": "https://rabota.by/vacancy/" + match.group(1),
            "title": link.get_text(" ", strip=True),
            "company": company.get_text(" ", strip=True) if company else "",
            "city": address.get_text(" ", strip=True).split(",")[0] if address else "",
            "remote": card.select_one('[data-qa="vacancy-label-work-schedule-remote"]') is not None,
        })
    return found


def search_praca(query):
    url = f"https://praca.by/search/vacancies/?search%5Bquery%5D={quote(query)}"
    soup = BeautifulSoup(fetch(url), "html.parser")
    found = []
    for link in soup.select("a.vac-small__title-link"):
        match = re.search(r"/vacancy/(\d+)", link.get("href", ""))
        card = link.find_parent(class_="vac-small")
        if not match or card is None:
            continue
        company = card.select_one(".vac-small__organization")
        city = card.select_one(".vac-small__city")
        found.append({
            "source": "praca.by",
            "id": "praca:" + match.group(1),
            "url": f"https://praca.by/vacancy/{match.group(1)}/",
            "title": link.get_text(" ", strip=True),
            "company": company.get_text(" ", strip=True) if company else "",
            "city": city.get_text(" ", strip=True) if city else "",
            "remote": "удал" in card.get_text(" ", strip=True).lower(),
        })
    return found


SOURCES = {"rabota.by": search_rabota, "praca.by": search_praca}
DESCRIPTION_SELECTORS = {
    "rabota.by": '[data-qa="vacancy-description"]',
    "praca.by": ".vacancy__description",
}


# ---------- отбор ----------

def word_pattern(word):
    """«маркет» найдёт «маркетолог»; короткие слова («pr», «ux») — только целиком."""
    word = re.escape(word.lower())
    if len(word) <= 3:
        return rf"(?<!\w){word}(?!\w)"
    return rf"(?<!\w){word}"


def find_category(title, config):
    title = title.lower()
    if any(re.search(word_pattern(w), title) for w in config.get("stop_words") or []):
        return None
    for category in sorted(config["categories"], key=lambda c: c["priority"]):
        if any(re.search(word_pattern(w), title) for w in category["title"]):
            return category
    return None


def same_vacancy_key(vacancy):
    """Ключ, по которому одна и та же вакансия узнаётся на разных сайтах."""
    def norm(text):
        text = ORG_FORMS.sub(" ", text.lower().replace("ё", "е"))
        return re.sub(r"[^\w]+", " ", text).strip()
    return norm(vacancy["title"]) + "|" + norm(vacancy["company"])


def collect_candidates(config, sent):
    sent_ids, sent_keys = set(sent["ids"]), set(sent["keys"])
    candidates, problems = {}, []
    for source, search in SOURCES.items():
        total, errors = 0, 0
        for category in config["categories"]:
            for query in category["search"]:
                try:
                    results = search(query)
                except Exception as err:  # один упавший запрос не должен ломать остальные
                    errors += 1
                    print(f"[{source}] ошибка при поиске «{query}»: {err}", file=sys.stderr)
                    continue
                total += len(results)
                for position, vacancy in enumerate(results):
                    key = same_vacancy_key(vacancy)
                    if vacancy["id"] in sent_ids or key in sent_keys or key in candidates:
                        continue
                    found_category = find_category(vacancy["title"], config)
                    if not found_category:
                        continue
                    vacancy.update(key=key, category=found_category["name"],
                                   priority=found_category["priority"], position=position)
                    candidates[key] = vacancy
        print(f"[{source}] найдено карточек: {total}, ошибок запросов: {errors}")
        if total == 0:
            problems.append(f"⚠️ {source}: не удалось получить ни одной вакансии — "
                            "возможно, сайт изменился или недоступен.")

    prefer = (config.get("prefer_city") or "").lower()

    def rank(v):
        nearby = v["remote"] or (prefer and v["city"].lower().startswith(prefer))
        return (v["priority"], 0 if nearby else 1, v["position"])

    return sorted(candidates.values(), key=rank), problems


# ---------- краткое описание ----------

def description_lines(container):
    """Превращает HTML описания в список строк: заголовки и пункты списков отдельно."""
    for br in container.find_all("br"):
        br.replace_with("\n")
    for tag in container.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    lines = []
    for line in container.get_text().split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(r"^([-–—•·*▪●✔✅]|\d+[.)])\s*", "", line).strip()
        if line:
            lines.append(line)
    return lines


def shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"


def summarize(lines, max_items=3, max_length=200):
    """Берёт первые пункты из блока «Обязанности». Возвращает (подпись, текст)."""
    for index, line in enumerate(lines):
        if len(line) <= 60 and DUTY_HEADING.search(line):
            items = []
            for item in lines[index + 1:]:
                if item.endswith(":") and len(item) <= 60:  # начался следующий раздел
                    break
                item = shorten(item.rstrip(";.,"), 110)
                if items and len("; ".join(items + [item])) > max_length:
                    break
                items.append(item)
                if len(items) == max_items:
                    break
            if items:
                items = [items[0][:1].upper() + items[0][1:]] + [
                    i[:1].lower() + i[1:] if i[1:2].islower() else i for i in items[1:]
                ]
                return "Что делать", "; ".join(items) + "."
    text = " ".join(lines)
    if not text:
        return "О вакансии", "подробности по ссылке."
    first_sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return "О вакансии", shorten(first_sentence, 200)


def add_summary(vacancy):
    try:
        soup = BeautifulSoup(fetch(vacancy["url"]), "html.parser")
        container = soup.select_one(DESCRIPTION_SELECTORS[vacancy["source"]])
        lines = description_lines(container) if container else []
    except Exception as err:
        print(f"Не удалось открыть {vacancy['url']}: {err}", file=sys.stderr)
        lines = []
    vacancy["summary_label"], vacancy["summary"] = summarize(lines)


# ---------- сообщение и отправка ----------

def build_message(picked, problems, today):
    esc = html.escape
    parts = [f"🗓 <b>Вакансии на {today:%d.%m}</b>"]
    if not picked:
        parts.append("Сегодня новых подходящих вакансий не нашлось.")
    for number, v in enumerate(picked, 1):
        where = "удалённо" if v["remote"] else v["city"]
        heading = " / ".join(x for x in [v["title"], v["company"]] if x)
        if where:
            heading += f", {where}"
        parts.append(
            f"{number}. <b>{esc(heading)}</b>\n"
            f"{esc(v['summary_label'])}: {esc(v['summary'])}\n"
            f"🔗 {esc(v['url'])}"
        )
    parts.extend(esc(p) for p in problems)
    return "\n\n".join(parts)


def send_telegram(text):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=30,
    )
    if not resp.ok:
        raise SystemExit(f"Telegram не принял сообщение: {resp.status_code} {resp.text}")


def load_sent():
    if SENT_FILE.exists():
        return json.loads(SENT_FILE.read_text(encoding="utf-8"))
    return {"ids": [], "keys": []}


def save_sent(sent, picked):
    sent["ids"] = (sent["ids"] + [v["id"] for v in picked])[-SENT_LIMIT:]
    sent["keys"] = (sent["keys"] + [v["key"] for v in picked])[-SENT_LIMIT:]
    SENT_FILE.write_text(json.dumps(sent, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main():
    dry_run = "--dry-run" in sys.argv
    if not dry_run and not (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        raise SystemExit("Не заданы TELEGRAM_TOKEN и TELEGRAM_CHAT_ID (см. README).")

    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    sent = load_sent()
    candidates, problems = collect_candidates(config, sent)
    print(f"Подходящих новых вакансий: {len(candidates)}")

    picked, companies = [], set()
    for vacancy in candidates:  # не больше одной вакансии от компании за день
        if len(picked) == config.get("per_day", 2):
            break
        company = same_vacancy_key(vacancy).split("|")[1]
        if company and company in companies:
            continue
        companies.add(company)
        picked.append(vacancy)
    for vacancy in picked:
        add_summary(vacancy)

    message = build_message(picked, problems, datetime.now(ZoneInfo("Europe/Minsk")))
    print("\n" + message + "\n")
    if dry_run:
        print("(--dry-run: ничего не отправлено и не сохранено)")
        return
    send_telegram(message)
    save_sent(sent, picked)
    print("Отправлено в Telegram.")


if __name__ == "__main__":
    main()
