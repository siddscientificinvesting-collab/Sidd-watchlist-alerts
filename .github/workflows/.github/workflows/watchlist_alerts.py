#!/usr/bin/env python3
"""
Watchlist alerts for NSE + BSE corporate announcements.

  python watchlist_alerts.py check            # fetch new filings, alert on Telegram, save PDFs
  python watchlist_alerts.py check --dry-run  # print alerts instead of sending, save nothing
  python watchlist_alerts.py summary          # daily summary on Telegram (email if SMTP_* is set)
  python watchlist_alerts.py sync-pdfs --to "H:\\Watchlist PDFs"   # download alerted PDFs to your PC
  python watchlist_alerts.py test-telegram    # send a test message
  python watchlist_alerts.py telegram-chat-id # print chat ids that messaged your bot

Settings come from environment variables (GitHub Secrets in the cloud):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  SMTP_USER, SMTP_PASSWORD, EMAIL_TO  (optional: SMTP_HOST, SMTP_PORT)
  SAVE_PDFS=1  SEND_PDF_TO_TELEGRAM=1  (set to 0 to turn off)
"""
import argparse
import csv
import html
import json
import os
import re
import smtplib
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
WATCHLIST_FILE = ROOT / "watchlist.csv"
STATE_FILE = ROOT / "state" / "state.json"
LOG_DIR = ROOT / "logs"
PDF_DIR = ROOT / "pdfs"

SAVE_PDFS = os.getenv("SAVE_PDFS", "1") == "1"
SEND_PDF_TO_TELEGRAM = os.getenv("SEND_PDF_TO_TELEGRAM", "1") == "1"
MAX_PDF_MB = 45            # Telegram bot upload limit is 50 MB
DUP_WINDOW_MIN = 60        # NSE and BSE copies of one filing land within this many minutes
DUP_MIN_SIMILARITY = 0.2   # ...and share at least this share of their key words
SEEN_KEEP_DAYS = 10
FAIL_WARN_STREAK = 3       # warn on Telegram after this many failed runs in a row

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
BROWSER_HINTS = {
    "sec-ch-ua": '"Google Chrome";v="141", "Chromium";v="141", "Not?A_Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
NSE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
    "X-Requested-With": "XMLHttpRequest",
    **BROWSER_HINTS,
}
BSE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    **BROWSER_HINTS,
}
NSE_API = "https://www.nseindia.com/api/corporate-announcements"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_ATTACH = ["https://www.bseindia.com/xml-data/corpfiling/AttachLive/",
              "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"]

LOG_FIELDS = ["time_ist", "exchange", "stock", "code", "category", "subject",
              "detail", "url", "pdf_path", "status", "found_ist"]


def now_ist() -> datetime:
    return datetime.now(IST)


def log(msg: str) -> None:
    print(f"{now_ist():%H:%M:%S} {msg}", flush=True)


# ── watchlist + state ────────────────────────────────────────────────────────
def load_watchlist() -> list[dict]:
    rows = []
    with open(WATCHLIST_FILE, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
            if not r.get("name") or r["name"].startswith("#"):
                continue
            r["nse_symbol"] = r.get("nse_symbol", "").upper()
            rows.append(r)
    if not rows:
        sys.exit("watchlist.csv has no stocks")
    return rows


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": {}, "fail_streak": {}, "warned": {}, "last_email": ""}


def save_state(state: dict) -> None:
    cutoff = (now_ist() - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v["t"] >= cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def append_log(rows: list[dict]) -> None:
    if not rows:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"announcements_{now_ist():%Y-%m}.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


# ── NSE ──────────────────────────────────────────────────────────────────────
def nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    for url in ("https://www.nseindia.com/",
                "https://www.nseindia.com/companies-listing/corporate-filings-announcements"):
        try:
            s.get(url, timeout=20)          # sets the cookies NSE's API needs
        except requests.RequestException:
            pass
        time.sleep(1.5)
    return s


def fetch_nse(watch: list[dict], days: int) -> tuple[list[dict], requests.Session]:
    by_sym = {w["nse_symbol"]: w for w in watch if w.get("nse_symbol")}
    by_isin = {w["isin"]: w for w in watch if w.get("isin")}
    params = {"index": "equities",
              "from_date": f"{now_ist() - timedelta(days=days):%d-%m-%Y}",
              "to_date": f"{now_ist():%d-%m-%Y}"}
    last_err = None
    for attempt in range(2):
        s = nse_session()
        try:
            r = s.get(NSE_API, params=params, timeout=40)
            r.raise_for_status()
            data = r.json()
            break
        except (requests.RequestException, ValueError) as e:
            last_err = e
            time.sleep(5)
    else:
        raise RuntimeError(f"NSE blocked or down: {last_err}")

    rows = data if isinstance(data, list) else data.get("data", [])
    items = []
    for a in rows:
        w = by_sym.get((a.get("symbol") or "").upper()) or by_isin.get(a.get("sm_isin") or "")
        if not w:
            continue
        try:
            t = datetime.strptime(a["sort_date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        except (KeyError, ValueError):
            t = now_ist()
        items.append({
            "key": f"NSE:{a.get('seq_id') or a.get('dt')}:{a.get('symbol')}",
            "exchange": "NSE", "stock": w["name"], "code": a.get("symbol", ""),
            "category": html.unescape(a.get("desc", "")), "subject": html.unescape(a.get("desc", "")),
            "detail": html.unescape(a.get("attchmntText") or ""),
            "url": (a.get("attchmntFile") or "").strip(), "time": t,
        })
    log(f"NSE: {len(rows)} market filings, {len(items)} for watchlist")
    return items, s


# ── BSE ──────────────────────────────────────────────────────────────────────
def fetch_bse(watch: list[dict], days: int) -> tuple[list[dict], requests.Session]:
    s = requests.Session()
    s.headers.update(BSE_HEADERS)
    frm = f"{now_ist() - timedelta(days=days):%Y%m%d}"
    to = f"{now_ist():%Y%m%d}"
    items, errors = [], 0
    stocks = [w for w in watch if w.get("bse_code")]
    for w in stocks:
        page, got = 1, 0
        while True:
            params = {"pageno": str(page), "strCat": "-1", "strPrevDate": frm,
                      "strScrip": w["bse_code"], "strSearch": "P", "strToDate": to,
                      "strType": "C", "subcategory": "-1"}
            payload = None
            for attempt in range(3):            # BSE sometimes rejects a request once
                try:
                    r = s.get(BSE_API, params=params, timeout=30)
                    r.raise_for_status()
                    payload = r.json()
                    break
                except (requests.RequestException, ValueError) as e:
                    err = e
                    time.sleep(2 + 3 * attempt)
            if payload is None:
                errors += 1
                log(f"BSE {w['bse_code']} failed: {err}")
                break
            rows = payload.get("Table") or []
            for a in rows:
                try:
                    t = datetime.strptime(str(a.get("DT_TM") or a.get("NEWS_DT"))[:19],
                                          "%Y-%m-%dT%H:%M:%S").replace(tzinfo=IST)
                except ValueError:
                    t = now_ist()
                subj = re.sub(r"^.*?-\s*\d{6}\s*-\s*", "", html.unescape(a.get("NEWSSUB") or "")).strip()
                att = (a.get("ATTACHMENTNAME") or "").strip()
                items.append({
                    "key": f"BSE:{a.get('NEWSID')}",
                    "exchange": "BSE", "stock": w["name"], "code": w["bse_code"],
                    "category": a.get("CATEGORYNAME") or "", "subject": subj or a.get("NEWSSUB", ""),
                    "detail": html.unescape(a.get("MORE") or a.get("HEADLINE") or ""),
                    "url": BSE_ATTACH[0] + att if att else "", "attach": att, "time": t,
                })
            got += len(rows)
            total = int((payload.get("Table1") or [{}])[0].get("ROWCNT", 0) or 0)
            if not rows or got >= total:
                break
            page += 1
            time.sleep(0.5)
        time.sleep(0.5)
    if stocks and errors >= len(stocks):
        raise RuntimeError("BSE blocked or down for every stock")
    log(f"BSE: {len(items)} filings for {len(stocks)} watchlist stocks")
    return items, s


# ── PDFs ─────────────────────────────────────────────────────────────────────
def slug(text: str, n: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:n] or "filing"


def download_pdf(item: dict, session: requests.Session, out_dir: Path) -> Path | None:
    urls = [item["url"]] if item["exchange"] == "NSE" else [b + item.get("attach", "") for b in BSE_ATTACH]
    urls = [u for u in urls if u and not u.endswith("/")]
    if not urls:
        return None
    ext = os.path.splitext(urls[0].split("?")[0])[1].lower() or ".pdf"
    if ext in (".mp3", ".mp4", ".wav", ".m4a"):
        return None
    dest = (out_dir / f"{item['time']:%Y-%m}" / slug(item["stock"], 40) /
            f"{item['time']:%Y%m%d_%H%M}_{item['exchange']}_{slug(item['subject'])}{ext}")
    if dest.exists() and dest.stat().st_size:
        return dest
    for url in urls:
        try:
            r = session.get(url, timeout=60, stream=True)
            if r.status_code != 200:
                continue
            size = int(r.headers.get("Content-Length") or 0)
            if size > MAX_PDF_MB * 1024 * 1024:
                log(f"  PDF too large ({size // 1048576} MB), link only")
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
            if dest.stat().st_size:
                item["url"] = url
                return dest
        except requests.RequestException as e:
            log(f"  PDF download failed: {e}")
    return None


# ── Telegram ─────────────────────────────────────────────────────────────────
def tg_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def tg_call(method: str, data: dict, files: dict | None = None) -> bool:
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"
    for _ in range(3):
        try:
            r = requests.post(url, data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], **data},
                              files=files, timeout=60)
        except requests.RequestException as e:
            log(f"Telegram error: {e}")
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)) + 1)
            continue
        if not r.ok:
            log(f"Telegram {method} failed: {r.status_code} {r.text[:200]}")
        return r.ok
    return False


def tg_text(text: str) -> bool:
    return tg_call("sendMessage", {"text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"})


def format_alert(item: dict, max_len: int) -> str:
    e = html.escape
    code = f"{item['exchange']}: {item['code']}"
    head = f"🔔 <b>{e(item['stock'])}</b>  ({e(code)})\n<b>{e(item['subject'])}</b>"
    if item["category"] and item["category"] != item["subject"]:
        head += f"\n<i>{e(item['category'])}</i>"
    foot = f"\n🕒 {item['time']:%d-%b %H:%M} IST"
    if item.get("also_on"):
        foot += f"  ·  also filed on {item['also_on']}"
    if item["url"]:
        foot += f'\n📄 <a href="{e(item["url"], quote=True)}">Open filing</a>'
    room = max_len - len(head) - len(foot) - 10
    detail = item["detail"].strip()
    if len(detail) > room:
        detail = detail[:max(room, 0)].rsplit(" ", 1)[0] + "…"
    return head + (f"\n\n{e(detail)}" if detail else "") + foot


def send_alert(item: dict, pdf: Path | None, dry: bool) -> bool:
    if dry or not tg_configured():
        print("-" * 60)
        print(re.sub(r"<[^>]+>", "", format_alert(item, 1000)))
        if pdf:
            print(f"   [PDF {pdf.stat().st_size // 1024} KB: {pdf.name}]")
        return True
    if pdf and SEND_PDF_TO_TELEGRAM:
        with open(pdf, "rb") as f:
            ok = tg_call("sendDocument", {"caption": format_alert(item, 1000), "parse_mode": "HTML"},
                         files={"document": (pdf.name, f)})
        if ok:
            time.sleep(1.1)
            return True
    ok = tg_text(format_alert(item, 3500))
    time.sleep(1.1)
    return ok


# ── check command ────────────────────────────────────────────────────────────
STOPWORDS = set("""limited ltd company exchange informed regarding about announcement
regulation lodr under with that this from have been please find enclosed herewith dated
held pursuant attached refer submitted submission intimation sebi listing obligations
disclosure requirements regulations copy the and for are has inform""".split())


def key_words(item: dict) -> list[str]:
    name = set(re.findall(r"[a-z]+", item["stock"].lower()))
    words = re.findall(r"[a-z]{4,}", f"{item['subject']} {item['detail']}".lower())
    return sorted({w for w in words if w not in STOPWORDS and w not in name})[:40]


def similarity(a: list[str], b: list[str]) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def mark_duplicates(new_items: list[dict], state: dict) -> None:
    """The same filing usually lands on NSE and BSE minutes apart. Pair each new
    item with at most one similar-worded filing of the same stock on the other
    exchange within DUP_WINDOW_MIN; the later one of the pair is not alerted."""
    window = timedelta(minutes=DUP_WINDOW_MIN)
    pool = [{"ex": v["ex"], "stock": v["stock"], "t": datetime.fromisoformat(v["t"]),
             "key": k, "w": v.get("w", []), "paired": v.get("paired", False)}
            for k, v in state["seen"].items()]
    for it in sorted(new_items, key=lambda x: (x["time"], x["exchange"] != "NSE")):
        it["words"] = key_words(it)
        best, best_score = None, DUP_MIN_SIMILARITY
        for p in pool:
            if (p["paired"] or p["stock"] != it["stock"] or p["ex"] == it["exchange"]
                    or abs(p["t"] - it["time"]) > window):
                continue
            score = similarity(p["w"], it["words"])
            if abs(p["t"] - it["time"]) <= timedelta(minutes=10):
                score += DUP_MIN_SIMILARITY      # near-simultaneous: wording may differ
            if score >= best_score:
                best, best_score = p, score
        if best:
            best["paired"] = True
            it["duplicate_of"] = best["key"]
            if best["key"] in state["seen"]:
                state["seen"][best["key"]]["paired"] = True
        pool.append({"ex": it["exchange"], "stock": it["stock"], "t": it["time"],
                     "key": it["key"], "w": it["words"], "paired": bool(best)})


def health(state: dict, exchange: str, ok: bool, err: str, dry: bool) -> None:
    streak = 0 if ok else state["fail_streak"].get(exchange, 0) + 1
    state["fail_streak"][exchange] = streak
    today = f"{now_ist():%Y-%m-%d}"
    if streak >= FAIL_WARN_STREAK and state["warned"].get(exchange) != today:
        msg = (f"⚠️ {exchange} has failed {streak} checks in a row ({html.escape(err[:150])}). "
               f"Alerts from the other exchange continue.")
        if not dry and tg_configured() and tg_text(msg):
            state["warned"][exchange] = today
        log(msg)


def cmd_check(args) -> None:
    watch = load_watchlist()
    state = load_state()
    log(f"Watchlist: {len(watch)} stocks, looking back {args.days} day(s)")

    items, sessions = [], {}
    for name, fetch in (("NSE", fetch_nse), ("BSE", fetch_bse)):
        try:
            got, sessions[name] = fetch(watch, args.days)
            items += got
            health(state, name, True, "", args.dry_run)
        except Exception as e:  # keep going with the other exchange
            log(f"{name} FAILED: {e}")
            health(state, name, False, str(e), args.dry_run)

    # an exchange sometimes lists one filing twice under different ids
    uniq, sig = [], set()
    for it in items:
        s = (it["exchange"], it["stock"], it["subject"].lower(), f"{it['time']:%Y%m%d%H%M}")
        if s not in sig:
            sig.add(s)
            uniq.append(it)
    new = [it for it in uniq if it["key"] not in state["seen"]]
    new.sort(key=lambda x: (x["time"], x["exchange"] != "NSE"))
    log(f"New filings: {len(new)}")
    mark_duplicates(new, state)

    pdf_root = Path(tempfile.mkdtemp(prefix="wl_pdf_")) if args.dry_run or not SAVE_PDFS else PDF_DIR
    by_key = {it["key"]: it for it in new}
    for it in new:
        first = by_key.get(it.get("duplicate_of", ""))
        if first:
            first["also_on"] = it["exchange"]
    log_rows = []
    for it in new:
        status = "seeded"
        pdf = None
        if it.get("duplicate_of"):
            status = "duplicate"
        elif not args.seed:
            if it["url"]:
                pdf = download_pdf(it, sessions.get(it["exchange"]) or requests.Session(), pdf_root)
            status = "sent" if send_alert(it, pdf, args.dry_run) else "failed"
        if status != "failed":   # failed sends are retried next run
            state["seen"][it["key"]] = {"t": it["time"].isoformat(), "ex": it["exchange"],
                                        "stock": it["stock"], "w": it.get("words", []),
                                        "paired": bool(it.get("duplicate_of"))}
        rel = ""
        if pdf and not args.dry_run and SAVE_PDFS:
            rel = pdf.relative_to(ROOT).as_posix()
        log_rows.append({**it, "time_ist": f"{it['time']:%Y-%m-%d %H:%M}", "pdf_path": rel,
                         "detail": it["detail"][:500], "status": status,
                         "found_ist": f"{now_ist():%Y-%m-%d %H:%M}"})

    if args.dry_run:
        log(f"Dry run: nothing saved. PDFs (if any) are in {pdf_root}")
        return
    append_log(log_rows)
    save_state(state)
    log(f"Done: {sum(r['status'] == 'sent' for r in log_rows)} alerts sent")


# ── email command ────────────────────────────────────────────────────────────
def read_log_since(since: datetime) -> list[dict]:
    rows = []
    for path in sorted(LOG_DIR.glob("announcements_*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                found = r.get("found_ist") or r["time_ist"]
                t = datetime.strptime(found, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
                if t > since and r["status"] in ("sent", "failed"):
                    rows.append(r)
    return rows


def cmd_sync_pdfs(args) -> None:
    """Download every alerted filing listed in logs/ into a local folder (e.g. on H: drive)."""
    out = Path(args.to).expanduser().resolve()
    sessions: dict[str, requests.Session] = {}
    done = failed = 0
    for path in sorted(LOG_DIR.glob("announcements_*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["status"] != "sent" or not r["url"]:
                    continue
                ex = r["exchange"]
                if ex not in sessions:
                    sessions[ex] = nse_session() if ex == "NSE" else requests.Session()
                    if ex == "BSE":
                        sessions[ex].headers.update(BSE_HEADERS)
                item = {"exchange": ex, "stock": r["stock"], "subject": r["subject"],
                        "url": r["url"], "attach": r["url"].rsplit("/", 1)[-1],
                        "time": datetime.strptime(r["time_ist"], "%Y-%m-%d %H:%M")}
                if download_pdf(item, sessions[ex], out):
                    done += 1
                else:
                    failed += 1
    log(f"PDFs in {out}: {done} present/downloaded, {failed} could not be fetched")


def send_telegram_summary(rows: list[dict], since: datetime, bad: list[str], dry: bool) -> None:
    e = html.escape
    lines = [f"📋 <b>Daily watchlist summary · {now_ist():%d %b %Y}</b>",
             f"{len(rows)} new filing(s) since {since:%d %b %H:%M} IST"]
    stock = None
    for r in rows:
        if r["stock"] != stock:
            stock = r["stock"]
            lines.append(f"\n<b>{e(stock)}</b>")
        subj = e(r["subject"][:120])
        if r["url"]:
            subj = f'<a href="{e(r["url"], quote=True)}">{subj}</a>'
        lines.append(f"• {e(r['time_ist'][5:])} {e(r['exchange'])}: {subj}")
    lines.append(f"\n⚠️ {', '.join(bad)} failing on the latest checks." if bad
                 else "\n✅ NSE and BSE checks are healthy.")
    chunks, cur = [], ""
    for line in lines:                       # Telegram allows 4096 characters per message
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    if dry or not tg_configured():
        for c in chunks:
            print(re.sub(r"<[^>]+>", "", c))
        if not dry:
            sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return
    for c in chunks:
        if not tg_text(c):
            sys.exit("Telegram summary failed")
        time.sleep(1.1)
    log(f"Telegram summary sent: {len(rows)} filing(s)")


def cmd_email(args) -> None:
    """Daily summary: by email if SMTP_USER is set, otherwise on Telegram."""
    state = load_state()
    since = (datetime.fromisoformat(state["last_email"]) if state.get("last_email")
             else now_ist() - timedelta(days=1))
    rows = sorted(read_log_since(since), key=lambda r: (r["stock"], r["time_ist"]))
    e = html.escape
    repo = os.getenv("GITHUB_REPOSITORY")
    branch = os.getenv("GITHUB_REF_NAME", "main")

    parts = [f"<h2>Watchlist filings · {now_ist():%d %b %Y}</h2>",
             f"<p>{len(rows)} new filing(s) since {since:%d %b %H:%M} IST.</p>"]
    stock = None
    for r in rows:
        if r["stock"] != stock:
            if stock is not None:
                parts.append("</table>")
            stock = r["stock"]
            parts.append(f"<h3>{e(stock)}</h3><table cellpadding='6' style='border-collapse:collapse'>")
        link = f"<a href='{e(r['url'], quote=True)}'>filing</a>" if r["url"] else ""
        if r["pdf_path"] and repo:
            link += f" · <a href='https://github.com/{repo}/blob/{branch}/{e(r['pdf_path'])}'>saved PDF</a>"
        parts.append(f"<tr style='border-top:1px solid #ddd'><td>{e(r['time_ist'][5:])}</td>"
                     f"<td>{e(r['exchange'])}</td><td><b>{e(r['subject'])}</b><br>"
                     f"<small>{e(r['detail'][:300])}</small></td><td>{link}</td></tr>")
    if stock is not None:
        parts.append("</table>")
    bad = [ex for ex, n in state.get("fail_streak", {}).items() if n]
    parts.append(f"<p style='color:#a00'>Warning: {', '.join(bad)} failing on the latest checks.</p>"
                 if bad else "<p style='color:#777'>NSE and BSE checks are healthy.</p>")
    body = "\n".join(parts)

    subject = f"Watchlist: {len(rows)} new filing(s) · {now_ist():%d %b}"
    if not os.getenv("SMTP_USER"):          # no email set up: send the summary on Telegram
        send_telegram_summary(rows, since, bad, args.dry_run)
        if not args.dry_run:
            state["last_email"] = now_ist().isoformat()
            save_state(state)
        return
    if args.dry_run:
        print(subject)
        print(re.sub(r"<[^>]+>", " ", body))
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.getenv("EMAIL_TO", os.environ["SMTP_USER"])
    msg.set_content(re.sub(r"<[^>]+>", " ", body))
    msg.add_alternative(body, subtype="html")
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
    state["last_email"] = now_ist().isoformat()
    save_state(state)
    log(f"Email sent: {subject}")


# ── helpers ──────────────────────────────────────────────────────────────────
def cmd_test_telegram(_args) -> None:
    if not tg_configured():
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first")
    ok = tg_text("✅ Watchlist alerts are connected. You will get NSE/BSE filings here.")
    sys.exit(0 if ok else 1)


def cmd_chat_id(_args) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or sys.exit("Set TELEGRAM_BOT_TOKEN first")
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    chats = {(u.get("message") or u.get("channel_post") or {}).get("chat", {}).get("id")
             for u in r.get("result", [])}
    chats.discard(None)
    print("Chat ids:", ", ".join(map(str, chats)) if chats
          else "none yet - send any message to your bot first, then rerun")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--dry-run", action="store_true", help="print alerts, save nothing")
    c.add_argument("--seed", action="store_true", help="mark current filings as seen without alerting")
    c.add_argument("--days", type=int, default=1, help="look-back window in days (default 1)")
    c.set_defaults(fn=cmd_check)
    for name in ("summary", "email"):
        m = sub.add_parser(name, help="daily summary (Telegram, or email if SMTP is set)")
        m.add_argument("--dry-run", action="store_true")
        m.set_defaults(fn=cmd_email)
    sp = sub.add_parser("sync-pdfs")
    sp.add_argument("--to", default=str(PDF_DIR), help="folder to save PDFs in")
    sp.set_defaults(fn=cmd_sync_pdfs)
    sub.add_parser("test-telegram").set_defaults(fn=cmd_test_telegram)
    sub.add_parser("telegram-chat-id").set_defaults(fn=cmd_chat_id)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
