"""Парсер вакансий для канала «Клацай, працуй!».

Раз в день ищет вакансии на сайтах из SOURCES, выбирает новые по настройкам
из config.yaml и присылает их в Telegram: название, кратко что делать, ссылка.

Запуск:
    python parser.py            — найти вакансии и отправить в Telegram
    python parser.py --dry-run  — только показать сообщение, ничего не отправлять

Переменная окружения EXPERIENCE (например «1-3» или «без опыта, 1-3»)
временно заменяет фильтр опыта из config.yaml.
"""

import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
MAX_DETAIL_PAGES = 25  # сколько страниц вакансий открывать в поисках подходящих

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
REQUEST_PAUSE = 1.5  # секунд между запросами к одному сайту

DUTY_HEADING = re.compile(
    r"обязанност|задачи|чем (предстоит|нужно|будешь|будете)|что (нужно |предстоит )?делать"
    r"|функционал|responsibilit|what you.ll do",
    re.IGNORECASE,
)
BLOCK_TAGS = ["p", "li", "ul", "ol", "div", "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr"]
ORG_FORMS = re.compile(r"\b(ооо|оао|зао|одо|чуп|чтуп|уп|ип|ао|пао|сооо|иооо|ltd|llc)\b")

# Опыт работы: внутреннее обозначение → как пишется в config.yaml и в сообщении
EXPERIENCE_LABELS = {
    "none": "без опыта",
    "1-3": "1–3 года",
    "3-6": "3–6 лет",
    "6+": "от 6 лет",
}
EXPERIENCE_ALIASES = {
    "без опыта": "none", "нет опыта": "none", "0": "none",
    "1-3": "1-3", "3-6": "3-6", "6+": "6+", "6": "6+",
}
RABOTA_EXPERIENCE = {
    "noExperience": "none", "between1And3": "1-3",
    "between3And6": "3-6", "moreThan6": "6+",
}

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})


def fetch(url):
    """Скачивает страницу; при сбое пробует ещё раз. Возвращает (html, итоговый адрес)."""
    for attempt in range(3):
        try:
            time.sleep(REQUEST_PAUSE)
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text, resp.url
        except requests.RequestException as err:
            last_error = err
            time.sleep(3 * (attempt + 1))
    raise last_error


def soup_of(url):
    page, _ = fetch(url)
    return BeautifulSoup(page, "html.parser")


def text_of(element):
    return element.get_text(" ", strip=True) if element else ""


def vacancy(source, vacancy_id, url, title, company="", city="", remote=False,
            experience=None, snippet=""):
    return {
        "source": source, "id": vacancy_id, "url": url, "title": title,
        "company": company, "city": city, "remote": remote,
        "experience": experience or detect_experience(snippet), "snippet": snippet,
    }


# ---------- поиск на сайтах ----------
# Каждая функция получает поисковую фразу и возвращает список вакансий со страницы поиска.

def search_rabota(query):
    soup = soup_of(
        "https://rabota.by/search/vacancy?area=16&order_by=publication_time"
        f"&search_period=7&text={quote(query)}"
    )
    found = []
    for card in soup.select('[data-qa="vacancy-serp__vacancy"]'):
        link = card.select_one('[data-qa="serp-item__title"]')
        match = link and re.search(r"/vacancy/(\d+)", link.get("href", ""))
        if not match:
            continue
        experience = None
        for element in card.select('[data-qa^="vacancy-serp__vacancy-work-experience-"]'):
            experience = RABOTA_EXPERIENCE.get(element["data-qa"].rsplit("-", 1)[-1])
        address = text_of(card.select_one('[data-qa="vacancy-serp__vacancy-address"]'))
        found.append(vacancy(
            "rabota.by", "rabota:" + match.group(1),
            "https://rabota.by/vacancy/" + match.group(1),
            text_of(link),
            company=text_of(card.select_one('[data-qa="vacancy-serp__vacancy-employer-text"]')),
            city=address.split(",")[0],
            remote=card.select_one('[data-qa="vacancy-label-work-schedule-remote"]') is not None,
            experience=experience,
        ))
    return found


def search_praca(query):
    soup = soup_of(f"https://praca.by/search/vacancies/?search%5Bquery%5D={quote(query)}")
    found = []
    for link in soup.select("a.vac-small__title-link"):
        match = re.search(r"/vacancy/(\d+)", link.get("href", ""))
        card = link.find_parent(class_="vac-small")
        if not match or card is None:
            continue
        found.append(vacancy(
            "praca.by", "praca:" + match.group(1),
            f"https://praca.by/vacancy/{match.group(1)}/",
            text_of(link),
            company=text_of(card.select_one(".vac-small__organization")),
            city=text_of(card.select_one(".vac-small__city")),
            remote="удал" in text_of(card).lower(),
            snippet=text_of(card.select_one(".vac-small__experience")),
        ))
    return found


def search_belmeta(query):
    soup = soup_of(f"https://belmeta.com/vacansii?q={quote(query)}&l=")
    found = []
    for card in soup.select("article.job[data-id]"):
        link = card.select_one("a.job-title")
        if not link:
            continue
        region = card.select_one(".job-data.region")
        places = (region.get("title") or "").split(",") if region else []
        found.append(vacancy(
            "belmeta.com", "belmeta:" + card["data-id"],
            f"https://belmeta.com/jobdesc?id={card['data-id']}",
            text_of(link),
            company=text_of(card.select_one(".job-data.company")),
            city=places[1].strip() if len(places) > 1 else text_of(region),
            remote="удал" in text_of(card).lower(),
            snippet=text_of(card.select_one(".desc")),
        ))
    return found


def search_minsk_business(query):
    soup = soup_of(f"https://minsk.business/rabota/vakansii/?text={quote(query)}")
    found = []
    for card in soup.select("div.card"):
        link = card.select_one("a.card-title")
        match = link and re.search(r"vakansiya-(\d+)", link.get("href", ""))
        if not match:
            continue
        # Это копии вакансий rabota.by с теми же номерами: ссылку даём на rabota.by.
        found.append(vacancy(
            "minsk.business", "rabota:" + match.group(1),
            "https://rabota.by/vacancy/" + match.group(1),
            text_of(link),
            company=text_of(card.select_one(".card-subtitle")),
            city="Минск",
            remote="удал" in text_of(card).lower(),
            snippet=text_of(card.select_one(".card-text")),
        ))
    return found


def search_gorodrabot(query):
    soup = soup_of(f"https://belarus.gorodrabot.by/?q={quote(query)}")
    found = []
    for card in soup.select(".snippet.vacancy"):
        link = card.select_one("a.snippet__title-link")
        match = link and re.search(r"/advert/(\d+)", link.get("href", ""))
        if not match:
            continue
        found.append(vacancy(
            "gorodrabot.by", "gorodrabot:" + match.group(1),
            link["href"],
            text_of(link),
            company=text_of(card.select_one(".snippet__meta-item_company")),
            city=text_of(card.select_one(".snippet__meta-item_location")),
            remote="удал" in text_of(card).lower(),
            snippet=text_of(card.select_one(".snippet__desc")),
        ))
    return found


def search_careerist(query):
    soup = soup_of(f"https://minsk.careerist.ru/search/?category=vacancy&text={quote(query)}")
    found = []
    for link in soup.select("a.vacancyLink"):
        card = link.find_parent(class_="list")
        match = re.search(r"-(\d+)\.html", link.get("href", ""))
        if not match or card is None:
            continue
        company = text_of(card.select_one(".list-right"))
        texts = card.select(".list-block p.card-text")
        found.append(vacancy(
            "careerist.ru", "careerist:" + match.group(1),
            link["href"],
            text_of(link),
            company="" if "партнерские" in company.lower() else company,
            city=text_of(card.select_one(".room")),
            remote="удал" in text_of(card).lower(),
            snippet=text_of(texts[-1]) if texts else "",
        ))
    return found


def search_bebee(query):
    soup = soup_of(f"https://bebee.com/by/jobs?q={quote(query)}")
    found, seen = [], set()
    for link in soup.select('h3 a[href^="/by/jobs/"]'):
        href = link["href"]
        card = link.find_parent("div", class_="p-4")
        if href in seen or card is None:
            continue
        seen.add(href)
        pin = card.select_one("svg.lucide-map-pin")
        city = text_of(pin.parent) if pin else ""
        strings = list(card.stripped_strings)
        # Порядок текста в карточке: название, город, компания, описание…, дата
        after_city = strings[strings.index(city) + 1:] if city in strings else strings[1:]
        # Многие вакансии bebee — копии rabota.by с тем же номером: ссылку даём на rabota.by.
        rabota_id = re.search(r"techmap_by_(\d+)$", href)
        found.append(vacancy(
            "bebee.com",
            "rabota:" + rabota_id.group(1) if rabota_id else "bebee:" + href.rsplit("/", 1)[-1],
            "https://rabota.by/vacancy/" + rabota_id.group(1) if rabota_id else "https://bebee.com" + href,
            text_of(link),
            company=after_city[0] if after_city else "",
            city=city,
            remote="удал" in " ".join(strings).lower(),
            snippet=" ".join(after_city[1:-1]),
        ))
    return found


# Порядок важен: если одна вакансия нашлась на нескольких сайтах, ссылка будет на первый.
SOURCES = {
    "rabota.by": search_rabota,
    "praca.by": search_praca,
    "minsk.business": search_minsk_business,
    "belmeta.com": search_belmeta,
    "gorodrabot.by": search_gorodrabot,
    "careerist.ru": search_careerist,
    "bebee.com": search_bebee,
}
DESCRIPTION_SELECTORS = {
    "rabota.by": '[data-qa="vacancy-description"]',
    "praca.by": ".vacancy__description",
    "belmeta.com": "div.text",
    "gorodrabot.by": ".content__module",
    "careerist.ru": ".targetDesBG",
}


# ---------- опыт работы ----------

def detect_experience(text):
    """Определяет опыт по тексту: «без опыта», «опыт от 2 лет» и т. п."""
    text = (text or "").lower().replace("ё", "е")
    if re.search(r"без опыта|нет опыта|опыт[а-я]* (работы )?не (требуется|обязател|нужен|важен)"
                 r"|no experience", text):
        return "none"
    match = re.search(
        r"опыт[^.;!?\n]{0,50}?(\d+(?:[.,]\d+)?)\s*(?:-|–|—)?\s*(?:х|ти|ми|го)?\s*"
        r"(год|лет|мес)", text)
    if not match:
        return None
    years = float(match.group(1).replace(",", "."))
    if match.group(2) == "мес":
        years /= 12
    if years < 1:
        return "none"
    if years < 3:
        return "1-3"
    if years < 6:
        return "3-6"
    return "6+"


def experience_filter(config):
    """Какие варианты опыта подходят. Пустое множество — подходит любой."""
    raw = os.environ.get("EXPERIENCE", "").strip()
    if raw in ("", "как в config.yaml"):
        values = config.get("experience") or []
    elif raw == "любой":
        values = []
    else:
        values = raw.split(",")
    if isinstance(values, str):
        values = values.split(",")
    allowed = set()
    for value in values:
        key = re.sub(r"\s+", " ", str(value).lower().replace("–", "-").replace("—", "-")).strip()
        key = key.replace(" - ", "-")
        if key not in EXPERIENCE_ALIASES:
            raise SystemExit(f"Непонятный вариант опыта «{value}». "
                             "Можно: без опыта, 1-3, 3-6, 6+")
        allowed.add(EXPERIENCE_ALIASES[key])
    return allowed


def experience_ok(experience, allowed, allow_unknown):
    if not allowed:
        return True
    if experience is None:
        return allow_unknown
    return experience in allowed


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


def same_vacancy_key(item):
    """Ключ, по которому одна и та же вакансия узнаётся на разных сайтах."""
    def norm(text):
        text = ORG_FORMS.sub(" ", text.lower().replace("ё", "е"))
        return re.sub(r"[^\w]+", " ", text).strip()
    return norm(item["title"]) + "|" + norm(item["company"])


def search_source(source, search, queries):
    """Обходит один сайт по всем поисковым фразам. Ошибка одного запроса не ломает остальные."""
    results, errors = [], 0
    for query in queries:
        try:
            results.append(search(query))
        except Exception as err:
            errors += 1
            results.append([])
            print(f"[{source}] ошибка при поиске «{query}»: {err}", file=sys.stderr)
    total = sum(len(r) for r in results)
    print(f"[{source}] найдено карточек: {total}, ошибок запросов: {errors}")
    return results


def collect_candidates(config, sent, allowed, allow_unknown):
    sent_ids, sent_keys = set(sent["ids"]), set(sent["keys"])
    queries = [(c, q) for c in config["categories"] for q in c["search"]]

    # Сайты обходятся одновременно, но каждый — по одному запросу за раз.
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        futures = {
            source: pool.submit(search_source, source, search, [q for _, q in queries])
            for source, search in SOURCES.items()
        }
        per_source = {source: future.result() for source, future in futures.items()}

    candidates, seen_ids, problems = {}, set(), []
    for source, results in per_source.items():
        if not any(results):
            problems.append(f"⚠️ {source}: не удалось получить ни одной вакансии — "
                            "возможно, сайт изменился или недоступен.")
        for result in results:
            for position, item in enumerate(result):
                key = same_vacancy_key(item)
                if (item["id"] in sent_ids or item["id"] in seen_ids
                        or key in sent_keys or key in candidates):
                    continue
                seen_ids.add(item["id"])
                category = find_category(item["title"], config)
                if not category or not experience_ok(item["experience"], allowed, True):
                    continue
                item.update(key=key, category=category["name"],
                            priority=category["priority"], position=position)
                candidates[key] = item

    prefer = (config.get("prefer_city") or "").lower()

    def rank(v):
        nearby = v["remote"] or (prefer and v["city"].lower().startswith(prefer))
        known = v["experience"] is not None or not allowed
        return (v["priority"], 0 if nearby else 1, 0 if known else 1, v["position"])

    return sorted(candidates.values(), key=rank), problems


# ---------- страница вакансии и краткое описание ----------

def html_lines(fragment):
    """Превращает HTML описания в список строк: заголовки и пункты списков отдельно."""
    container = BeautifulSoup(fragment, "html.parser") if isinstance(fragment, str) else fragment
    for br in container.find_all("br"):
        br.replace_with("\n")
    for tag in container.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    lines = []
    for line in container.get_text().split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(r"^([-–—•·*▪●◆◇■□○◦►▸➤✔✅☑️]|\d+[.)])\s*", "", line).strip()
        if line:
            lines.append(line)
    return lines


def json_ld_description(soup):
    """Описание из разметки schema.org JobPosting — она есть на многих сайтах вакансий."""
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or script.get_text()).strip()
        raw = re.sub(r"^\s*//\s*<!--|//\s*-->\s*$", "", raw).strip()
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if item.get("@type") == "JobPosting" and item.get("description"):
                    return item["description"]
                stack.extend(item.get("@graph", []))
    return None


def shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"


def summarize(lines, max_items=3, max_length=200):
    """Берёт первые пункты из блока «Обязанности». Возвращает (подпись, текст) или None."""
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
                text = "; ".join(items)
                return "Что делать", text if text.endswith("…") else text + "."
    return None


def fallback_summary(text):
    if not text:
        return "О вакансии", "подробности по ссылке."
    text = text.strip(" .…")
    first_sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return "О вакансии", shorten(first_sentence, 200)


def load_details(item):
    """Открывает страницу вакансии: краткое описание и опыт, если его не было в поиске."""
    lines, page_text, final_url = [], "", item["url"]
    try:
        page, final_url = fetch(item["url"])
        soup = BeautifulSoup(page, "html.parser")
        description = json_ld_description(soup)
        if description:
            lines = html_lines(description)
        else:
            container = soup.select_one(DESCRIPTION_SELECTORS.get(item["source"], "body"))
            lines = html_lines(container or soup.body or soup)
        page_text = " ".join(lines)
    except Exception as err:
        print(f"Не удалось открыть {item['url']}: {err}", file=sys.stderr)

    if item["experience"] is None:
        item["experience"] = detect_experience(page_text)
    item["summary_label"], item["summary"] = (
        summarize(lines) or fallback_summary(item["snippet"] or " ".join(lines[:3]))
    )
    # Агрегаторы перенаправляют на сайт работодателя — даём прямую ссылку на него.
    if final_url and final_url != item["url"] and item["source"] in ("belmeta.com",):
        item["url"] = final_url


def pick(candidates, config, allowed, allow_unknown):
    """Выбирает вакансии на сегодня: подходящий опыт, не больше одной от компании."""
    picked, companies, opened = [], set(), 0
    per_day = config.get("per_day", 2)
    for item in candidates:
        if len(picked) == per_day or opened >= MAX_DETAIL_PAGES:
            break
        company = item["key"].split("|")[1]
        if company and company in companies:
            continue
        load_details(item)
        opened += 1
        if not experience_ok(item["experience"], allowed, allow_unknown):
            continue
        companies.add(company)
        picked.append(item)
    return picked


# ---------- сообщение и отправка ----------

def build_message(picked, problems, today, allowed):
    esc = html.escape
    title = f"🗓 <b>Вакансии на {today:%d.%m}</b>"
    if allowed:
        title += " · опыт: " + ", ".join(EXPERIENCE_LABELS[e] for e in EXPERIENCE_LABELS if e in allowed)
    parts = [title]
    if not picked:
        parts.append("Сегодня новых подходящих вакансий не нашлось.")
    for number, v in enumerate(picked, 1):
        where = "удалённо" if v["remote"] else v["city"]
        heading = " / ".join(x for x in [v["title"], v["company"]] if x)
        if where:
            heading += f", {where}"
        lines = [f"{number}. <b>{esc(heading)}</b>"]
        if v["experience"]:
            lines.append(f"Опыт: {esc(EXPERIENCE_LABELS[v['experience']])}")
        lines.append(f"{esc(v['summary_label'])}: {esc(v['summary'])}")
        lines.append(f"🔗 {esc(v['url'])}")
        parts.append("\n".join(lines))
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
    allowed = experience_filter(config)
    allow_unknown = config.get("experience_unknown", True)
    sent = load_sent()

    candidates, problems = collect_candidates(config, sent, allowed, allow_unknown)
    print(f"Подходящих новых вакансий: {len(candidates)}")
    picked = pick(candidates, config, allowed, allow_unknown)

    message = build_message(picked, problems, datetime.now(ZoneInfo("Europe/Minsk")), allowed)
    print("\n" + message + "\n")
    if dry_run:
        print("(--dry-run: ничего не отправлено и не сохранено)")
        return
    send_telegram(message)
    save_sent(sent, picked)
    print("Отправлено в Telegram.")


if __name__ == "__main__":
    main()
