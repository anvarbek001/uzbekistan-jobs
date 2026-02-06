import os, json, time, re
import requests
import hashlib

LT_URL = (os.environ.get("LT_URL") or "").strip()      # masalan: https://libretranslate.de/translate
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


def hh_get(path, params=None):
    r = requests.get(
        HH_BASE + path,
        params=params or {},
        headers={"HH-User-Agent": UA},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": False}

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


def clean_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

TECH_WORDS = [
    "PHP","Laravel","MySQL","PostgreSQL","SQL","NoSQL","Redis","MongoDB",
    "JavaScript","TypeScript","React","Vue","Angular","Node.js","NodeJS",
    "HTML","CSS","SASS","SCSS","Bootstrap","Tailwind","jQuery",
    "Python","Django","Flask","FastAPI","Java","Spring","C#",".NET","ASP.NET",
    "Go","Golang","Rust","C++","C","Kotlin","Swift",
    "Docker","Kubernetes","Git","GitHub","GitLab","CI/CD","Linux","Nginx","Apache",
    "REST","GraphQL","API","SOAP","Kafka","RabbitMQ","AWS","GCP","Azure"
]

def lt_translate_ru_to_uz(text: str, state: dict) -> str:
    """LibreTranslate orqali RU->UZ. LT_URL bo'lmasa textni qaytaradi."""
    if not text or not LT_URL:
        return text

    key = hashlib.md5(text.encode("utf-8")).hexdigest()
    cached = state["tr_cache"].get(key)
    if cached:
        return cached

    payload = {"q": text, "source": "ru", "target": "uz", "format": "text"}
    if LT_API_KEY:
        payload["api_key"] = LT_API_KEY

    translated = text
    try:
        r = requests.post(LT_URL, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(3)
        r.raise_for_status()
        translated = r.json().get("translatedText") or text
    except Exception:
        translated = text

    # cache kattalashib ketmasin
    if len(state["tr_cache"]) > 4000:
        state["tr_cache"] = {}
    state["tr_cache"][key] = translated
    save_state(state)
    return translated

def mask_tokens(text: str):
    """Tech/url/email/raqamlarni placeholderga almashtiradi."""
    tokens = []

    def put(m):
        tokens.append(m.group(0))
        return f"__TK{len(tokens)-1}__"

    # URL
    text = re.sub(r"https?://\S+", put, text)
    # Email
    text = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", put, text)
    # Raqamlar (maosh, yil, foiz)
    text = re.sub(r"\b\d+[.,]?\d*\b", put, text)

    # Texnologiyalar (case-insensitive)
    for w in sorted(TECH_WORDS, key=len, reverse=True):
        pattern = r"(?i)\b" + re.escape(w) + r"\b"
        text = re.sub(pattern, put, text)

    return text, tokens

def unmask_tokens(text: str, tokens):
    for i, t in enumerate(tokens):
        text = text.replace(f"__TK{i}__", t)
    return text

def smart_translate(text: str, state: dict) -> str:
    """RU->UZ, lekin texnologiya/URL/raqamlarni saqlab qoladi."""
    if not text:
        return text
    masked, tokens = mask_tokens(text)
    tr = lt_translate_ru_to_uz(masked, state)
    return unmask_tokens(tr, tokens)



def extract_tech(item) -> str:
    req = clean_html((item.get("snippet") or {}).get("requirement") or "")
    if not req:
        return ""
    if len(req) > 120:
        req = req[:120].rstrip() + "…"
    return req


def domain_from_url(u: str) -> str:
    if not u:
        return ""
    u = u.replace("https://", "").replace("http://", "")
    return u.split("/")[0]


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

    area = item.get("area") or {}
    return area.get("name") or "O‘zbekiston"


def salary_text(item) -> str:
    salary = item.get("salary") or {}
    if not salary:
        return ""
    frm = salary.get("from")
    to = salary.get("to")
    cur = salary.get("currency", "")

    if frm and to:
        return f"{frm}-{to} {cur}"
    if frm:
        return f"{frm}+ {cur}"
    if to:
        return f"≤ {to} {cur}"
    return ""


def guess_lang(item) -> str:
    req = clean_html((item.get("snippet") or {}).get("requirement") or "").lower()
    if any(w in req for w in ["англий", "english", "en "]):
        return "EN"
    if any(w in req for w in ["узбек", "o'zbek", "uzbek", "uz "]):
        return "UZ"
    if any(w in req for w in ["русск", "ru "]):
        return "RU"
    return ""


def find_uzbekistan_area_id():
    areas = hh_get("/areas", {"host": HOST})
    targets = {"узбекистан", "uzbekistan", "oʻzbekiston", "o'zbekiston", "ozbekiston"}

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


def main():
    if not UA:
        raise RuntimeError("HH_USER_AGENT bo‘sh. Secrets’da HH_USER_AGENT ni to‘g‘ri qo‘ying.")

    state = load_state()
    posted = set(state.get("posted_ids", []))

    area_id = find_uzbekistan_area_id()
    if not area_id:
        raise RuntimeError("Uzbekistan area_id topilmadi.")

    data = hh_get("/vacancies", {
        "host": HOST,
        "area": area_id,
        "per_page": 100,
        "page": 0,
        "order_by": "publication_time",
    })

    items = data.get("items") or []

    # yangi (yuborilmagan) vakansiyalar
    fresh = []
    for it in items:
        vid = it.get("id")
        if not vid:
            continue
        if vid not in posted:
            fresh.append(it)

    # eski->yangi tartibda yuboramiz
    fresh.sort(key=lambda x: x.get("published_at") or x.get("created_at") or "")

    sent_count = 0
    for it in fresh[:MAX_POSTS]:
        vid = it.get("id")
        title_raw = it.get("name", "Vakansiya")
        title = smart_translate(title_raw, state)
        
        tech_raw = extract_tech(it)  # bu snippet.requirement (ruscha bo'lishi mumkin)
        tech = smart_translate(tech_raw, state) if tech_raw else ""

        url = it.get("alternate_url") or it.get("url") or ""

        employer_obj = it.get("employer") or {}
        employer = employer_obj.get("name", "")
        employer_url = employer_obj.get("alternate_url") or ""
        employer_domain = domain_from_url(employer_url)

        company_line = employer
        if employer and employer_domain:
            company_line = f"{employer} ({employer_domain})"

        loc = best_location(it)
        sal = salary_text(it)

        experience = (it.get("experience") or {}).get("name", "")
        schedule = (it.get("schedule") or {}).get("name", "")
        employment = (it.get("employment") or {}).get("name", "")
        work_type = " | ".join([p for p in [schedule, employment] if p])

        tech = extract_tech(it)
        lang = guess_lang(it)

        lines = [
            f"💼 {title}",
            f"🏢 Kompaniya: {company_line}" if employer else None,
            f"💵 Maosh: {sal}" if sal else "💵 Maosh: Kelishiladi",
            f"💼 Tajriba: {experience}" if experience else None,
            f"🛠 Texnologiya: {tech}" if tech else None,
            f"🌐 Format: {work_type}" if work_type else None,
            f"📍 Manzil: {loc}" if loc else None,
            f"🇺🇿 Til: {lang}" if lang else None,
            f"🔗 Batafsil: {url}" if url else None,
        ]

        text = "\n".join([l for l in lines if l])
        tg_send(text)

        posted.add(vid)
        sent_count += 1
        time.sleep(SLEEP_BETWEEN)

    # posted_ids kattalashib ketmasin (oxirgilarini qoldiramiz)
    state["posted_ids"] = list(posted)[-5000:]
    save_state(state)

    # Hech narsa topilmasa — xato qilmay chiqib ketadi
    return


if __name__ == "__main__":
    main()
