import os, json, time, re, hashlib
import requests

LT_URL = (os.environ.get("LT_URL") or "").strip()
LT_API_KEY = (os.environ.get("LT_API_KEY") or "").strip()

HH_BASE = "https://api.hh.ru"

UA = (os.environ.get("HH_USER_AGENT", "UzJobsBot/1.0 (your_email@example.com)") or "")
UA = UA.strip().replace("\r", " ").replace("\n", " ")

TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
HOST = os.environ.get("HH_HOST", "hh.uz")

STATE_FILE = "state.json"
MAX_POSTS = int(os.environ.get("MAX_POSTS", "10"))
SLEEP_BETWEEN = float(os.environ.get("SLEEP_BETWEEN_POSTS", "2"))


# ---------- HELPERS ----------
def fmt_num(n):
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)


def fmt_salary(frm, to, cur):
    cur_map = {"UZS": "so'm", "RUB": "₽", "RUR": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}
    c = cur_map.get(cur, cur or "")

    if frm is not None and to is not None:
        return f"{fmt_num(frm)}–{fmt_num(to)} {c}".strip()
    if frm is not None:
        return f"{fmt_num(frm)}+ {c}".strip()
    if to is not None:
        return f"≤ {fmt_num(to)} {c}".strip()
    return ""


def salary_text(item):
    salary = item.get("salary") or {}
    return fmt_salary(salary.get("from"), salary.get("to"), salary.get("currency"))


def clean_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def html_escape(s: str) -> str:
    if not s:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def domain_from_url(u):
    if not u:
        return ""
    return u.replace("https://", "").replace("http://", "").split("/")[0]


def best_location(item) -> str:
    addr = item.get("address")
    if isinstance(addr, dict):
        raw = addr.get("raw")
        if raw:
            return raw
        parts = [addr.get("city"), addr.get("street"), addr.get("building")]
        parts = [p for p in parts if p]
        if parts:
            return ", ".join(parts)
    return (item.get("area") or {}).get("name") or ""


def extract_tech(item) -> str:
    req = clean_html((item.get("snippet") or {}).get("requirement") or "")
    if not req:
        return ""
    if len(req) > 120:
        req = req[:120].rstrip() + "…"
    return req


def guess_lang(item) -> str:
    req = clean_html((item.get("snippet") or {}).get("requirement") or "").lower()
    if any(w in req for w in ["англий", "english", "en "]):
        return "EN"
    if any(w in req for w in ["узбек", "o'zbek", "uzbek", "uz "]):
        return "UZ"
    if any(w in req for w in ["русск", "ru "]):
        return "RU"
    return ""


# ---------- TELEGRAM ----------
def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    for _ in range(6):
        r = requests.post(url, json=payload, timeout=30)

        if r.status_code == 429:
            try:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                retry_after = 5
            time.sleep(retry_after + 1)
            continue

        r.raise_for_status()
        return


# ---------- STATE ----------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"posted_ids": [], "tr_cache": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)
    s.setdefault("posted_ids", [])
    s.setdefault("tr_cache", {})
    return s


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- TRANSLATE ----------
def smart_translate(text, state):
    # LT_URL bo'lmasa tarjima qilmaydi
    if not text or not LT_URL:
        return text

    key = hashlib.md5(text.encode("utf-8")).hexdigest()
    if key in state["tr_cache"]:
        return state["tr_cache"][key]

    payload = {"q": text, "source": "ru", "target": "uz", "format": "text"}
    if LT_API_KEY:
        payload["api_key"] = LT_API_KEY

    tr = text
    try:
        r = requests.post(LT_URL, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(3)
        r.raise_for_status()
        tr = r.json().get("translatedText") or text
    except Exception:
        tr = text

    # cache kattalashib ketmasin
    if len(state["tr_cache"]) > 4000:
        state["tr_cache"] = {}
    state["tr_cache"][key] = tr
    save_state(state)
    return tr


# ---------- HH AREA (UZ) ----------
def find_uzbekistan_area_id():
    areas = requests.get(
        HH_BASE + "/areas",
        params={"host": HOST},
        headers={"HH-User-Agent": UA},
        timeout=30
    ).json()

    targets = {"uzbekistan", "oʻzbekiston", "o'zbekiston", "ozbekiston", "узбекистан"}

    def walk(nodes):
        for n in nodes:
            name = (n.get("name") or "").strip().lower()
            if name in targets:
                return n.get("id")
            child = walk(n.get("areas") or [])
            if child:
                return child
        return None

    return walk(areas)


# ---------- MAIN ----------
def main():
    if not UA:
        raise RuntimeError("HH_USER_AGENT bo‘sh. Secrets’da HH_USER_AGENT ni to‘g‘ri qo‘ying.")

    state = load_state()
    posted = set(state.get("posted_ids", []))

    area_id = find_uzbekistan_area_id()
    if not area_id:
        raise RuntimeError("Uzbekistan area_id topilmadi.")

    data = requests.get(
        HH_BASE + "/vacancies",
        params={"host": HOST, "area": area_id, "per_page": 100, "page": 0, "order_by": "publication_time"},
        headers={"HH-User-Agent": UA},
        timeout=30
    ).json()

    items = data.get("items") or []

    fresh = [i for i in items if i.get("id") and i["id"] not in posted]
    fresh.sort(key=lambda x: x.get("published_at") or x.get("created_at") or "")

    for it in fresh[:MAX_POSTS]:
        vid = it["id"]

        url = it.get("alternate_url") or it.get("url") or ""

        title_raw = it.get("name") or "Vakansiya"
        title = smart_translate(title_raw, state)

        tech_raw = extract_tech(it)
        tech = smart_translate(tech_raw, state) if tech_raw else ""

        employer_obj = it.get("employer") or {}
        employer = employer_obj.get("name") or ""
        employer_domain = domain_from_url(employer_obj.get("alternate_url") or "")

        company_line = employer
        if employer and employer_domain:
            company_line = f"{employer} ({employer_domain})"

        sal = salary_text(it)
       experience_raw = (it.get("experience") or {}).get("name", "")
        schedule_raw = (it.get("schedule") or {}).get("name", "")
        employment_raw = (it.get("employment") or {}).get("name", "")
        
        # bular odatda ruscha keladi → tarjima qilamiz
        experience = smart_translate(experience_raw, state) if experience_raw else ""
        schedule = smart_translate(schedule_raw, state) if schedule_raw else ""
        employment = smart_translate(employment_raw, state) if employment_raw else ""
        
        work_type = " | ".join([p for p in [schedule, employment] if p])


        loc = best_location(it)
        lang = guess_lang(it)

        # HTML safe
        title_safe = html_escape(title)
        company_safe = html_escape(company_line)
        experience_safe = html_escape(experience)
        work_type_safe = html_escape(work_type)
        loc_safe = html_escape(loc)
        tech_safe = html_escape(tech)
        sal_safe = html_escape(sal)
        lang_safe = html_escape(lang)
        url_safe = html_escape(url)

        lines = [
            f"💼 <b>{title_safe}</b>",
            f"🏢 Kompaniya: {company_safe}" if employer else None,
            f"💵 Maosh: {sal_safe}" if sal else "💵 Maosh: Kelishiladi",
            f"💼 Tajriba: {experience_safe}" if experience else None,
            f"🛠 Texnologiya: {tech_safe}" if tech else None,
            f"🌐 Format: {work_type_safe}" if work_type else None,
            f"📍 Manzil: {loc_safe}" if loc else None,
            f"🇺🇿 Til: {lang_safe}" if lang else None,
           f"🔗 <a href=\"{url_safe}\">Murojaat qilish</a>" if url else None,
            f"🆔 ID: {vid}",
        ]

        text = "\n".join([l for l in lines if l])
        tg_send(text)

        posted.add(vid)
        time.sleep(SLEEP_BETWEEN)

    state["posted_ids"] = list(posted)[-5000:]
    save_state(state)


if __name__ == "__main__":
    main()
