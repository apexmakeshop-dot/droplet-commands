# auto_prospect.py
# Path: ~/.openclaw/workspace/auto_prospect.py
# Generated: 2026-03-25T20:22:09.521783+00:00

#!/usr/bin/env python3
"""
auto_prospect.py
Autonomous prospect finder using People Data Labs + Hunter.io
Runs at 9am via cron, feeds results to OpenClaw agent
"""

import os
import csv
import glob
import json
import time
import httpx
import random
import subprocess
from datetime import datetime

os.environ.setdefault('PDL_API_KEY', '[REDACTED]')
os.environ.setdefault('HUNTER_API_KEY', '[REDACTED]')

PDL_KEY     = os.getenv("PDL_API_KEY")
HUNTER_KEY  = os.getenv("HUNTER_API_KEY")
WORKSPACE   = os.path.expanduser("~/.openclaw/workspace")
OPENCLAW    = os.path.expanduser("~/.nvm/versions/node/v22.22.1/bin/openclaw")
TELEGRAM_ID = "8235811059"
TARGET      = 10   # genuinely new prospects to collect
MAX_PAGES   = 50   # max scroll depth (50 pages × 10 = 500 candidates)
CRON_LOG    = os.path.expanduser("~/openclaw-cron.log")
CREDIT_FILE = os.path.expanduser("~/.openclaw/pdl_credits.json")
CREDIT_WARN = 250
CREDIT_MAX  = 350


# ── PDL Credit Tracking ───────────────────────────────────────────────────────

def load_credits():
    """Load monthly PDL credit counter. Resets on new month."""
    now = datetime.utcnow()
    month_key = now.strftime("%Y-%m")
    try:
        with open(CREDIT_FILE) as f:
            data = json.load(f)
        if data.get("month") != month_key:
            data = {"month": month_key, "calls": 0, "warned": False}
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"month": month_key, "calls": 0, "warned": False}
    return data

def save_credits(data):
    os.makedirs(os.path.dirname(CREDIT_FILE), exist_ok=True)
    with open(CREDIT_FILE, "w") as f:
        json.dump(data, f)

def log_credit_used(label=""):
    """Log a PDL credit use and check warning threshold."""
    data = load_credits()
    data["calls"] += 1
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    log_line = f"{timestamp} PDL CREDIT USED [{label}] — monthly total: {data['calls']}/{CREDIT_MAX}\n"
    print(log_line.strip())
    with open(CRON_LOG, "a") as f:
        f.write(log_line)

    if data["calls"] >= CREDIT_WARN and not data.get("warned"):
        data["warned"] = True
        warn_msg = (f"⚠️ PDL credit warning: {data['calls']} of {CREDIT_MAX} monthly credits used "
                    f"(threshold: {CREDIT_WARN}). Prospector will stop working at {CREDIT_MAX}. "
                    f"Check your PDL dashboard to review usage.")
        subprocess.run([OPENCLAW, "agent", "--to", TELEGRAM_ID, "--message", warn_msg, "--deliver"])
        print(f"WARNING: PDL credit threshold reached ({data['calls']}/{CREDIT_MAX}) — Telegram alert sent")

    save_credits(data)
    return data["calls"]


# ── Deduplication ─────────────────────────────────────────────────────────────

def load_seen_contacts():
    """Load all names and LinkedIn URLs from existing inbox CSVs to deduplicate."""
    seen_names    = set()
    seen_linkedin = set()
    pattern = os.path.join(WORKSPACE, "inbox", "auto_prospects_*.csv")
    for csv_path in glob.glob(pattern):
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = (row.get("Name") or "").strip().lower()
                    linkedin = (row.get("LinkedIn") or "").strip().lower()
                    if name and name != "unknown":
                        seen_names.add(name)
                    if linkedin and linkedin not in ("not found", ""):
                        seen_linkedin.add(linkedin)
        except Exception as e:
            print(f"Warning: could not read {csv_path}: {e}")
    print(f"Loaded {len(seen_names)} seen names, {len(seen_linkedin)} seen LinkedIn URLs from history")
    return seen_names, seen_linkedin


def is_duplicate(p, seen_names, seen_linkedin):
    name     = (p.get("full_name") or "").strip().lower()
    linkedin = (p.get("linkedin_url") or "").strip().lower()
    if name and name != "unknown" and name in seen_names:
        return True
    if linkedin and linkedin not in ("not found", "") and linkedin in seen_linkedin:
        return True
    return False


# ── PDL Search ────────────────────────────────────────────────────────────────

PDL_QUERY = {
    "bool": {
        "must": [
            {"terms": {"job_title_role": ["sales"]}},
            {"terms": {"job_title_levels": ["manager", "director", "vp"]}},
            {"range": {"job_company_employee_count": {"gte": 10, "lte": 100}}},
            {"terms": {"job_company_industry": ["computer software", "internet"]}},
            {"term": {"job_company_type": "private"}},
        ]
    }
}

def pdl_request(size, scroll_token=None, label="scroll"):
    """Make a single PDL API call, log credit, return (data, scroll_token)."""
    log_credit_used(label)
    url = "https://api.peopledatalabs.com/v5/person/search"
    payload = {"query": PDL_QUERY, "size": size, "pretty": True}
    if scroll_token:
        payload["scroll_token"] = scroll_token
    headers = {"X-Api-Key": PDL_KEY, "Content-Type": "application/json"}
    r = httpx.post(url, json=payload, headers=headers, timeout=30)
    if r.status_code == 429:
        return None, None, 429
    if r.status_code != 200:
        print(f"PDL error: {r.status_code} {r.text[:200]}")
        return [], None, r.status_code
    resp = r.json()
    return resp.get("data", []), resp.get("scroll_token"), 200

def pdl_scroll(scroll_token=None):
    data, token, status = pdl_request(10, scroll_token, label="collect")
    return (data or []), token


def find_prospects(seen_names, seen_linkedin):
    """Scroll PDL with a date-seeded start page, collecting unique prospects."""
    today_seed = int(datetime.utcnow().strftime("%Y%m%d"))
    rng = random.Random(today_seed)
    pages_to_skip = rng.randint(0, 49)
    print(f"Date seed {today_seed}: skipping {pages_to_skip} pages before collecting")

    scroll_token = None
    records_to_skip = pages_to_skip * 10
    skipped = 0

    while skipped < records_to_skip:
        batch = min(100, records_to_skip - skipped)
        data, scroll_token, status = pdl_request(batch, scroll_token, label="skip")
        if status == 429:
            print("Rate limited during skip — waiting 5s")
            time.sleep(5)
            continue
        if status != 200:
            print(f"PDL error during skip: {status}")
            break
        skipped += batch
        if not scroll_token:
            print(f"PDL scroll exhausted after skipping {skipped} records")
            break
        time.sleep(0.5)

    new_prospects = []
    pages_searched = 0
    while len(new_prospects) < TARGET and pages_searched < MAX_PAGES:
        data, scroll_token = pdl_scroll(scroll_token)
        pages_searched += 1
        for p in data:
            if not is_duplicate(p, seen_names, seen_linkedin):
                new_prospects.append(p)
                seen_names.add((p.get("full_name") or "").lower())
                linkedin = p.get("linkedin_url", "")
                if linkedin:
                    seen_linkedin.add(linkedin.lower())
            if len(new_prospects) >= TARGET:
                break
        if not scroll_token:
            print("PDL scroll exhausted — no more pages available")
            break
        time.sleep(0.3)

    print(f"Searched {pages_searched} pages after skip, found {len(new_prospects)} new prospects")
    return new_prospects


# ── Email & Output ────────────────────────────────────────────────────────────

def verify_email(first, last, domain):
    """Verify email via Hunter.io"""
    url = "https://api.hunter.io/v2/email-verifier"
    email = f"{first.lower()}.{last.lower()}@{domain}"
    params = {"email": email, "api_key": HUNTER_KEY}
    r = httpx.get(url, params=params, timeout=15)
    if r.status_code == 200:
        result = r.json().get("data", {})
        return result.get("result") in ["deliverable", "risky"], email
    return False, email


def build_prospect_list(raw):
    prospects = []
    for p in raw:
        try:
            name     = p.get("full_name", "Unknown")
            title    = p.get("job_title", "Unknown")
            company  = p.get("job_company_name", "Unknown")
            domain   = p.get("job_company_website", "").replace("http://","").replace("https://","").strip("/")
            linkedin = p.get("linkedin_url", "Not found")
            size     = p.get("job_company_employee_count", "Unknown")
            industry = p.get("job_company_industry", "Unknown")
            first = p.get("first_name", "")
            last  = p.get("last_name", "")
            verified, email = verify_email(first, last, domain) if domain else (False, "Unknown")
            prospects.append({
                "name": name, "title": title, "company": company,
                "domain": domain, "email": email, "email_verified": verified,
                "linkedin": linkedin, "size": size, "industry": industry
            })
        except Exception:
            continue
    return prospects


def save_and_notify(prospects):
    if not prospects:
        print("No prospects found")
        return

    path = f"{WORKSPACE}/inbox/auto_prospects_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    with open(path, "w") as f:
        f.write("Name,Title,Company,Domain,Email,Email_Verified,LinkedIn,Size,Industry\n")
        for p in prospects:
            f.write(f"{p['name']},{p['title']},{p['company']},{p['domain']},"
                    f"{p['email']},{p['email_verified']},{p['linkedin']},"
                    f"{p['size']},{p['industry']}\n")

    print(f"Saved {len(prospects)} prospects to {path}")

    # Report monthly credit usage in notification
    credit_data = load_credits()
    msg = (f"A new prospect file has been automatically generated and saved to your inbox: "
           f"auto_prospects_{datetime.utcnow().strftime('%Y%m%d')}.csv. "
           f"It contains {len(prospects)} pre-verified contacts from People Data Labs "
           f"(PDL credits used this month: {credit_data['calls']}/{CREDIT_MAX}). "
           f"Please read the file, research each company using Brave Search for buying signals, "
           f"and draft personalised outreach messages for each using our parallel outreach protocol. "
           f"Present all drafts to me for approval.")

    subprocess.run([OPENCLAW, "agent", "--to", TELEGRAM_ID, "--message", msg, "--deliver"])


if __name__ == "__main__":
    print(f"Auto-prospector running at {datetime.utcnow()}")
    seen_names, seen_linkedin = load_seen_contacts()
    raw = find_prospects(seen_names, seen_linkedin)
    print(f"Found {len(raw)} new raw results from PDL")
    prospects = build_prospect_list(raw)
    print(f"Processed {len(prospects)} prospects")
    save_and_notify(prospects)


================================================================================

# auto_prospect_starter.py
# Path: ~/.openclaw/workspace/auto_prospect_starter.py

#!/usr/bin/env python3
"""
auto_prospect_starter.py
Finds Starter tier prospects: freelancers, solo consultants,
small agencies who need B2B research on a budget.
Runs at 10am via cron, one hour after the Growth tier prospector.
"""

import os
import csv
import glob
import json
import time
import httpx
import random
import subprocess
from datetime import datetime

os.environ.setdefault('PDL_API_KEY', '[REDACTED]')
os.environ.setdefault('HUNTER_API_KEY', '[REDACTED]')

PDL_KEY     = os.getenv("PDL_API_KEY")
HUNTER_KEY  = os.getenv("HUNTER_API_KEY")
WORKSPACE   = os.path.expanduser("~/.openclaw/workspace-starter")
OPENCLAW    = os.path.expanduser("~/.nvm/versions/node/v22.22.1/bin/openclaw")
TELEGRAM_ID = "8235811059"
TARGET      = 10
MAX_PAGES   = 50
CRON_LOG    = os.path.expanduser("~/openclaw-cron.log")
CREDIT_FILE = os.path.expanduser("~/.openclaw/pdl_credits.json")  # shared with growth tier
CREDIT_WARN = 250
CREDIT_MAX  = 350


# ── PDL Credit Tracking ───────────────────────────────────────────────────────

def load_credits():
    """Load monthly PDL credit counter. Resets on new month."""
    now = datetime.utcnow()
    month_key = now.strftime("%Y-%m")
    try:
        with open(CREDIT_FILE) as f:
            data = json.load(f)
        if data.get("month") != month_key:
            data = {"month": month_key, "calls": 0, "warned": False}
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"month": month_key, "calls": 0, "warned": False}
    return data

def save_credits(data):
    os.makedirs(os.path.dirname(CREDIT_FILE), exist_ok=True)
    with open(CREDIT_FILE, "w") as f:
        json.dump(data, f)

def log_credit_used(label=""):
    """Log a PDL credit use and check warning threshold."""
    data = load_credits()
    data["calls"] += 1
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    log_line = f"{timestamp} PDL CREDIT USED [{label}] — monthly total: {data['calls']}/{CREDIT_MAX}\n"
    print(log_line.strip())
    with open(CRON_LOG, "a") as f:
        f.write(log_line)

    if data["calls"] >= CREDIT_WARN and not data.get("warned"):
        data["warned"] = True
        warn_msg = (f"⚠️ PDL credit warning: {data['calls']} of {CREDIT_MAX} monthly credits used "
                    f"(threshold: {CREDIT_WARN}). Prospector will stop working at {CREDIT_MAX}. "
                    f"Check your PDL dashboard to review usage.")
        subprocess.run([
            OPENCLAW, "agent", "--agent", "starter",
            "--to", TELEGRAM_ID, "--message", warn_msg, "--deliver",
            "--reply-channel", "telegram", "--reply-account", "starterprospectagentbot"
        ])
        # Also alert on main agent
        subprocess.run([OPENCLAW, "agent", "--to", TELEGRAM_ID, "--message", warn_msg, "--deliver"])
        print(f"WARNING: PDL credit threshold reached ({data['calls']}/{CREDIT_MAX}) — Telegram alert sent")

    save_credits(data)
    return data["calls"]


# ── Deduplication ─────────────────────────────────────────────────────────────

def load_seen_contacts():
    seen_names    = set()
    seen_linkedin = set()
    pattern = os.path.join(WORKSPACE, "inbox", "starter_prospects_*.csv")
    for csv_path in glob.glob(pattern):
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = (row.get("Name") or "").strip().lower()
                    linkedin = (row.get("LinkedIn") or "").strip().lower()
                    if name and name != "unknown":
                        seen_names.add(name)
                    if linkedin and linkedin not in ("not found", ""):
                        seen_linkedin.add(linkedin)
        except Exception as e:
            print(f"Warning: could not read {csv_path}: {e}")
    print(f"Loaded {len(seen_names)} seen names, {len(seen_linkedin)} seen LinkedIn URLs from history")
    return seen_names, seen_linkedin


def is_duplicate(p, seen_names, seen_linkedin):
    name     = (p.get("full_name") or "").strip().lower()
    linkedin = (p.get("linkedin_url") or "").strip().lower()
    if name and name != "unknown" and name in seen_names:
        return True
    if linkedin and linkedin not in ("not found", "") and linkedin in seen_linkedin:
        return True
    return False


# ── PDL Search ────────────────────────────────────────────────────────────────

PDL_QUERY = {
    "bool": {
        "must": [
            {"terms": {"job_title_levels": ["owner", "founder", "partner"]}},
            {"range": {"job_company_employee_count": {"gte": 1, "lte": 15}}},
            {"terms": {"job_company_industry": [
                "marketing and advertising",
                "management consulting",
                "computer software",
                "internet",
                "public relations and communications",
                "staffing and recruiting",
                "graphic design",
                "financial services"
            ]}}
        ]
    }
}

def pdl_request(size, scroll_token=None, label="scroll"):
    """Make a single PDL API call, log credit, return (data, scroll_token, status)."""
    log_credit_used(label)
    url = "https://api.peopledatalabs.com/v5/person/search"
    payload = {"query": PDL_QUERY, "size": size, "pretty": True}
    if scroll_token:
        payload["scroll_token"] = scroll_token
    headers = {"X-Api-Key": PDL_KEY, "Content-Type": "application/json"}
    r = httpx.post(url, json=payload, headers=headers, timeout=30)
    if r.status_code == 429:
        return None, None, 429
    if r.status_code != 200:
        print(f"PDL error: {r.status_code} {r.text[:200]}")
        return [], None, r.status_code
    resp = r.json()
    return resp.get("data", []), resp.get("scroll_token"), 200

def pdl_scroll(scroll_token=None):
    data, token, status = pdl_request(10, scroll_token, label="collect")
    return (data or []), token


def find_prospects(seen_names, seen_linkedin):
    today_seed = int(datetime.utcnow().strftime("%Y%m%d"))
    rng = random.Random(today_seed)
    pages_to_skip = rng.randint(0, 49)
    print(f"Date seed {today_seed}: skipping {pages_to_skip} pages before collecting")

    scroll_token = None
    records_to_skip = pages_to_skip * 10
    skipped = 0

    while skipped < records_to_skip:
        batch = min(100, records_to_skip - skipped)
        data, scroll_token, status = pdl_request(batch, scroll_token, label="skip")
        if status == 429:
            print("Rate limited during skip — waiting 5s")
            time.sleep(5)
            continue
        if status != 200:
            print(f"PDL error during skip: {status}")
            break
        skipped += batch
        if not scroll_token:
            print(f"PDL scroll exhausted after skipping {skipped} records")
            break
        time.sleep(0.5)

    new_prospects = []
    pages_searched = 0
    while len(new_prospects) < TARGET and pages_searched < MAX_PAGES:
        data, scroll_token = pdl_scroll(scroll_token)
        pages_searched += 1
        for p in data:
            if not is_duplicate(p, seen_names, seen_linkedin):
                new_prospects.append(p)
                seen_names.add((p.get("full_name") or "").lower())
                linkedin = p.get("linkedin_url", "")
                if linkedin:
                    seen_linkedin.add(linkedin.lower())
            if len(new_prospects) >= TARGET:
                break
        if not scroll_token:
            print("PDL scroll exhausted — no more pages available")
            break
        time.sleep(0.3)

    print(f"Searched {pages_searched} pages after skip, found {len(new_prospects)} new prospects")
    return new_prospects


# ── Email & Output ────────────────────────────────────────────────────────────

def verify_email(first, last, domain):
    url = "https://api.hunter.io/v2/email-verifier"
    email = f"{first.lower()}.{last.lower()}@{domain}"
    params = {"email": email, "api_key": HUNTER_KEY}
    r = httpx.get(url, params=params, timeout=15)
    if r.status_code == 200:
        result = r.json().get("data", {})
        return result.get("result") in ["deliverable", "risky"], email
    return False, email


def build_prospect_list(raw):
    prospects = []
    for p in raw:
        try:
            name     = p.get("full_name", "Unknown")
            title    = p.get("job_title", "Unknown")
            company  = p.get("job_company_name", "Unknown")
            domain   = p.get("job_company_website", "").replace("http://","").replace("https://","").strip("/")
            linkedin = p.get("linkedin_url", "Not found")
            size     = p.get("job_company_employee_count", "Unknown")
            industry = p.get("job_company_industry", "Unknown")
            first    = p.get("first_name", "")
            last     = p.get("last_name", "")
            verified, email = verify_email(first, last, domain) if domain else (False, "Unknown")
            prospects.append({
                "name": name, "title": title, "company": company,
                "domain": domain, "email": email, "email_verified": verified,
                "linkedin": linkedin, "size": size, "industry": industry,
                "recommended_tier": "Starter $149"
            })
        except Exception:
            continue
    return prospects


def save_and_notify(prospects):
    if not prospects:
        print("No starter prospects found")
        return

    path = f"{WORKSPACE}/inbox/starter_prospects_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    with open(path, "w") as f:
        f.write("Name,Title,Company,Domain,Email,Email_Verified,LinkedIn,Size,Industry,Tier\n")
        for p in prospects:
            f.write(f"{p['name']},{p['title']},{p['company']},{p['domain']},"
                    f"{p['email']},{p['email_verified']},{p['linkedin']},"
                    f"{p['size']},{p['industry']},{p['recommended_tier']}\n")

    print(f"Saved {len(prospects)} starter prospects to {path}")

    credit_data = load_credits()
    msg = (
        f"New STARTER TIER prospect file ready: "
        f"starter_prospects_{datetime.utcnow().strftime('%Y%m%d')}.csv. "
        f"{len(prospects)} freelancers and small agency owners ready for outreach "
        f"(PDL credits used this month: {credit_data['calls']}/{CREDIT_MAX}). "
        f"Please read the file, research each contact, and draft personalised "
        f"outreach for the Starter tier at $149. Key message: we save them "
        f"4 to 8 hours of manual research per client for just $149. "
        f"Present all drafts for my approval before sending."
    )

    subprocess.run([
        OPENCLAW, "agent", "--agent", "starter",
        "--to", TELEGRAM_ID, "--message", msg, "--deliver",
        "--reply-channel", "telegram", "--reply-account", "starterprospectagentbot"
    ])


if __name__ == "__main__":
    print(f"Starter auto-prospector running at {datetime.utcnow()}")
    seen_names, seen_linkedin = load_seen_contacts()
    raw = find_prospects(seen_names, seen_linkedin)
    print(f"Found {len(raw)} new raw results from PDL")
    prospects = build_prospect_list(raw)
    print(f"Processed {len(prospects)} prospects")
    save_and_notify(prospects)

