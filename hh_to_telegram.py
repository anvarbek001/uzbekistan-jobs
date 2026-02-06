import os, json
import time
import requests


HH_BASE = "https://api.hh.ru"
UA = (os.environ.get("HH_USER_AGENT", "UzJobsBot/1.0 (your_email@example.com)") or "")
UA = UA.strip().replace("\r", " ").replace("\n", " ")
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]  # public kanal bo‘lsa: @kanal_username
HOST = os.environ.get("HH_HOST", "hh.uz")
STATE_FILE = "state.json"

def hh_get(path, params=None):
    r = requests.get(
        HH_BASE + path,
        params=params or {},
        headers={"HH-User-Agent": UA},   # MUHIM!
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def tg_send(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for attempt in range(5):
        r = requests.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False
        }, timeout=30)

        if r.status_code == 429:
            # Telegram odatda "retry_after" beradi
            try:
                retry_after = r.json().get("parameters", {}).get("retry_after", 5)
            except Exception:
                retry_after = 5
            time.sleep(int(retry_after) + 1)
            continue

        r.raise_for_status()
        return


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_published_at": None}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

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

def best_location(item):
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

def main():
    state = load_state()
    last = state.get("last_published_at")

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
    new_items = []
    for it in items:
        pub = it.get("published_at") or it.get("created_at")
        if not pub:
            continue
        if last is None or pub > last:
            new_items.append(it)

    new_items.sort(key=lambda x: x.get("published_at") or x.get("created_at") or "")
    MAX_POSTS = int(os.environ.get("MAX_POSTS", "10"))
    for idx, it in enumerate(new_items[:MAX_POSTS], start=1):
        title = it.get("name", "Vakansiya")
        url = it.get("alternate_url") or it.get("url") or ""
        employer = (it.get("employer") or {}).get("name", "")

        salary = it.get("salary") or {}
        sal_txt = ""
        if salary:
            frm, to, cur = salary.get("from"), salary.get("to"), salary.get("currency", "")
            if frm and to: sal_txt = f"{frm}–{to} {cur}"
            elif frm:      sal_txt = f"{frm}+ {cur}"
            elif to:       sal_txt = f"≤ {to} {cur}"

        loc = best_location(it)

 # Qo‘shimcha maydonlar
experience = (it.get("experience") or {}).get("name", "")
schedule = (it.get("schedule") or {}).get("name", "")
employment = (it.get("employment") or {}).get("name", "")

tech = ""
snippet = it.get("snippet") or {}
if snippet:
    tech = snippet.get("requirement") or ""

salary = salary_text(it)

lines = [
    f"💼 {title}",
    f"🏢 Kompaniya: {employer}" if employer else None,
    f"💵 Maosh: {salary}" if salary else "💵 Maosh: Kelishiladi",
    f"💼 Tajriba: {experience}" if experience else None,
    f"🛠 Texnologiya: {tech[:120]}" if tech else None,
    f"🌐 Format: {schedule or employment}" if (schedule or employment) else None,
    f"📍 Manzil: {loc}" if loc else None,
    f"🔗 Batafsil: {url}" if url else None,
    "#ish #vakansiya #uzbekiston",
]

text = "\n".join([l for l in lines if l])


        tg_send("\n".join([l for l in lines if l]))

    if items:
        newest = (items[0].get("published_at") or items[0].get("created_at"))
        if newest:
            state["last_published_at"] = newest
            save_state(state)

if __name__ == "__main__":
    main()
