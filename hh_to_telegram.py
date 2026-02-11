import os, json, time, re, hashlib
import requests
from datetime import datetime, timedelta
import fcntl

LT_URL = (os.environ.get("LT_URL") or "").strip()
LT_API_KEY = (os.environ.get("LT_API_KEY") or "").strip()

HH_BASE = "https://api.hh.ru"

UA = (os.environ.get("HH_USER_AGENT", "UzJobsBot/1.0 (your_email@example.com)") or "")
UA = UA.strip().replace("\r", " ").replace("\n", " ")

TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
HOST = os.environ.get("HH_HOST", "hh.uz")

STATE_FILE = "state.json"
LOCK_FILE = "bot.lock"
MAX_POSTS = int(os.environ.get("MAX_POSTS", "10"))
SLEEP_BETWEEN = float(os.environ.get("SLEEP_BETWEEN_POSTS", "2"))
DAYS_TO_KEEP = int(os.environ.get("DAYS_TO_KEEP", "14"))  # 14 kun


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
        return f"from {fmt_num(frm)} {c}".strip()
    if to is not None:
        return f"to {fmt_num(to)} {c}".strip()
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
        return {"posted_with_time": {}, "tr_cache": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)
    
    # Eski formatdan yangi formatga o'tish
    if "posted_ids" in s and "posted_with_time" not in s:
        s["posted_with_time"] = {vid: datetime.now().isoformat() for vid in s.get("posted_ids", [])}
        s.pop("posted_ids", None)
    
    s.setdefault("posted_with_time", {})
    s.setdefault("tr_cache", {})
    return s


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def clean_old_entries(state):
    """Eski ID'lar va tarjima cache'ni tozalash"""
    now = datetime.now()
    cutoff = now - timedelta(days=DAYS_TO_KEEP)
    
    # Eski ID'larni o'chirish
    posted_with_time = state.get("posted_with_time", {})
    cleaned = {
        vid: timestamp 
        for vid, timestamp in posted_with_time.items()
        if datetime.fromisoformat(timestamp) > cutoff
    }
    state["posted_with_time"] = cleaned
    
    # Tarjima cache'ni cheklash (eng ko'pi 2000 ta)
    tr_cache = state.get("tr_cache", {})
    if len(tr_cache) > 2000:
        state["tr_cache"] = {}
    
    return set(cleaned.keys())


# ---------- TRANSLATE ----------
def smart_translate(text, state):
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

    state["tr_cache"][key] = tr
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
        raise RuntimeError("HH_USER_AGENT bo'sh. Secrets'da HH_USER_AGENT ni to'g'ri qo'ying.")

    # Lock file - parallel ishga tushishni oldini olish
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⚠️ Boshqa jarayon ishlayapti. To'xtatildi.")
        lock_fd.close()
        return

    try:
        state = load_state()
        posted = clean_old_entries(state)

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

        print(f"📊 Jami: {len(items)} | Yangi: {len(fresh)} | Yuboriladi: {min(len(fresh), MAX_POSTS)}")

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
            
            try:
                tg_send(text)
                print(f"✅ Yuborildi: {vid} - {title_raw}")
                
                # Har safar saqlash - xatolik bo'lsa ham oldingilari saqlanadi
                posted.add(vid)
                state["posted_with_time"][vid] = datetime.now().isoformat()
                save_state(state)
                
                time.sleep(SLEEP_BETWEEN)
            except Exception as e:
                print(f"❌ Xatolik: {vid} - {str(e)}")
                continue

    finally:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)


if __name__ == "__main__":
    main()
