"""
Job alert bot for Senior Backend / MERN Stack roles.
Searches Adzuna + JSearch (Google Jobs aggregator: LinkedIn, Naukri, Indeed, etc.),
scores each job against the resume skills, skips already-sent jobs,
and sends new matches to Telegram.
"""
import json
import os
import re
import html
from pathlib import Path

import requests

# ---------------- CONFIG (edit freely) ----------------
SEARCH_QUERIES = [
    "Senior Backend Developer Node.js",
    "Senior Node.js Developer",
    "MERN Stack Developer",
    "NestJS Developer",
    "Backend Engineer TypeScript",
]
LOCATIONS = ["Chennai"]   # add "Bangalore" or "Remote" here later if you want more
MIN_SCORE = 3          # minimum number of matching skills to notify
MAX_PER_RUN = 25       # cap notifications per run

# Skills from the resume, used to score how well a job matches
SKILLS = [
    "node", "node.js", "nodejs", "typescript", "javascript", "nestjs", "nest.js",
    "express", "react", "next.js", "mern", "mongodb", "mysql", "postgres",
    "postgresql", "redis", "rabbitmq", "kafka", "bullmq", "microservices",
    "event-driven", "aws", "docker", "kubernetes", "ci/cd", "github actions",
    "jwt", "oauth", "keycloak", "prisma", "llm", "rest api", "spring boot",
]
# Titles that are clearly not a fit
EXCLUDE_TITLE = ["intern", "fresher", "trainee", "junior", "php only", "sales", "android", "ios"]

SEEN_FILE = Path(__file__).parent / "seen_jobs.json"

ADZUNA_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_KEY = os.getenv("ADZUNA_APP_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")


# ---------------- SOURCES ----------------
def search_adzuna(query, location):
    if not (ADZUNA_ID and ADZUNA_KEY):
        return []
    params = {
        "app_id": ADZUNA_ID, "app_key": ADZUNA_KEY,
        "what": query, "results_per_page": 30,
        "max_days_old": 1, "sort_by": "date",
    }
    if location.lower() != "remote":
        params["where"] = location
    else:
        params["what"] = f"{query} remote"
    try:
        r = requests.get("https://api.adzuna.com/v1/api/jobs/in/search/1", params=params, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[adzuna] {query}/{location}: {e}")
        return []
    jobs = []
    for j in r.json().get("results", []):
        jobs.append({
            "id": f"adz-{j.get('id')}",
            "title": j.get("title", ""),
            "company": (j.get("company") or {}).get("display_name", ""),
            "location": (j.get("location") or {}).get("display_name", ""),
            "url": j.get("redirect_url", ""),
            "desc": j.get("description", ""),
            "source": "Adzuna",
        })
    return jobs


def search_jsearch(query, location):
    if not RAPIDAPI_KEY:
        return []
    q = f"{query} in {location}, India" if location.lower() != "remote" else f"{query} remote India"
    params = {"query": q, "page": 1, "num_pages": 1, "date_posted": "today", "country": "in"}
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    try:
        r = requests.get("https://jsearch.p.rapidapi.com/search", params=params, headers=headers, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[jsearch] {q}: {e}")
        return []
    jobs = []
    for j in r.json().get("data", []):
        loc = "Remote" if j.get("job_is_remote") else ", ".join(
            x for x in [j.get("job_city"), j.get("job_state")] if x)
        jobs.append({
            "id": f"js-{j.get('job_id')}",
            "title": j.get("job_title", ""),
            "company": j.get("employer_name", ""),
            "location": loc,
            "url": j.get("job_apply_link", ""),
            "desc": j.get("job_description", ""),
            "source": j.get("job_publisher", "JSearch"),
        })
    return jobs


# ---------------- MATCHING ----------------
def score(job):
    text = f"{job['title']} {job['desc']}".lower()
    hits = sorted({s for s in SKILLS if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", text)})
    return len(hits), hits


def is_excluded(job):
    t = job["title"].lower()
    return any(x in t for x in EXCLUDE_TITLE)


def dedupe_key(job):
    # same job posted on multiple sites -> treat as one
    return re.sub(r"\W+", "", f"{job['title']}{job['company']}".lower())


# ---------------- STATE ----------------
def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen):
    # keep the file from growing forever
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:], indent=0))


# ---------------- TELEGRAM ----------------
def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        print(text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=30,
    )
    if not r.ok:
        print("Telegram error:", r.text)


def format_job(job, n, hits):
    e = html.escape
    return (f"<b>{e(job['title'])}</b>\n"
            f"🏢 {e(job['company'])}  📍 {e(job['location'] or 'N/A')}\n"
            f"✅ Match {n}: {e(', '.join(hits[:8]))}\n"
            f"🔗 <a href=\"{e(job['url'])}\">Apply ({e(job['source'])})</a>")


# ---------------- MAIN ----------------
def main():
    seen = load_seen()
    found, keys = [], set()

    for q in SEARCH_QUERIES:
        for loc in LOCATIONS:
            for job in search_adzuna(q, loc) + search_jsearch(q, loc):
                k = dedupe_key(job)
                if not job["url"] or k in keys or k in seen or is_excluded(job):
                    continue
                n, hits = score(job)
                if n >= MIN_SCORE:
                    keys.add(k)
                    found.append((n, hits, job, k))

    found.sort(key=lambda x: x[0], reverse=True)
    found = found[:MAX_PER_RUN]

    if not found:
        print("No new matching jobs this run.")
        return

    send_telegram(f"🔔 <b>{len(found)} new backend / MERN jobs</b>")
    batch = ""
    for n, hits, job, k in found:
        block = format_job(job, n, hits) + "\n\n"
        if len(batch) + len(block) > 3800:   # Telegram limit is 4096 chars
            send_telegram(batch)
            batch = ""
        batch += block
        seen.add(k)
    if batch:
        send_telegram(batch)

    save_seen(seen)
    print(f"Sent {len(found)} jobs.")


if __name__ == "__main__":
    main()
