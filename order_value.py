"""
Order size vs revenue, for "Orders & contracts" filings.

- order_value_cr(text): the order amount in ₹ crore, read from the filing text / PDF text.
- Revenue: NSE integrated-filing results (XBRL) first, revenue.csv as a backup.
  Quarterly = latest reported quarter; annual = sum of the last 4 quarters (TTM).
"""
from __future__ import annotations

import csv
import io
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REVENUE_CSV = ROOT / "revenue.csv"
NSE_RESULTS = "https://www.nseindia.com/api/integrated-filing-results"

# approximate rates for orders quoted in foreign currency (override with env vars)
FX = {"USD": float(os.getenv("USD_INR", "88")), "EUR": float(os.getenv("EUR_INR", "102")),
      "GBP": float(os.getenv("GBP_INR", "117")), "AED": float(os.getenv("AED_INR", "24"))}

# ── order amount ─────────────────────────────────────────────────────────────
_NUM = r"(\d+(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_MONEY = re.compile(
    r"(?P<cur>₹|rs\s?\.?|inr|rupees|usd|us\s?\$|\$|eur|€|gbp|£|aed)\s*" + _NUM +
    r"\s*(?P<unit>lakh crores?|lakh crore|crores?|cr\.?|cr\b|lakhs?|lacs?|million|mn\.?|billion|bn\.?)?",
    re.I)
_CONTEXT_BAD = re.compile(r"order ?book|backlog|turnover|revenue|net ?worth|market cap|share capital|"
                          r"paid[- ]up|total income|profit|authori[sz]ed capital|face value|per share|"
                          r"multinational|is a (usd|us\$|\$|inr|rs)|group of|day rate|per day|per month|"
                          r"per annum|per unit|per tonne|per kg|per mw|rate of", re.I)
_CONTEXT_GOOD = re.compile(r"order|contract|worth|valued|value|loa\b|letter of|award|consideration|"
                           r"bid|tender|project", re.I)
_CONTEXT_TOTAL = re.compile(r"aggregat|total (order|contract|value)|in aggregate|cumulative(ly)? (value|worth)", re.I)
# L&T and a few others give a size band instead of a value (₹ crore, lower bound of the band)
_BANDS = [("ultra-mega", 15000), ("ultra mega", 15000), ("mega", 10000), ("major", 5000),
          ("large", 2500), ("significant", 1000)]


def _to_crore(cur: str, num: str, unit: str) -> float | None:
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    cur = cur.lower().replace(" ", "")
    unit = (unit or "").lower().rstrip(".")
    fx = 1.0
    if cur in ("usd", "us$", "$"):
        fx = FX["USD"]
    elif cur in ("eur", "€"):
        fx = FX["EUR"]
    elif cur in ("gbp", "£"):
        fx = FX["GBP"]
    elif cur == "aed":
        fx = FX["AED"]
    if unit.startswith("lakh crore"):
        rupees_cr = v * 100000
    elif unit.startswith(("crore", "cr")):
        rupees_cr = v
    elif unit.startswith(("lakh", "lac")):
        rupees_cr = v / 100
    elif unit.startswith(("million", "mn")):
        rupees_cr = v / 10
    elif unit.startswith(("billion", "bn")):
        rupees_cr = v * 100
    elif not unit and v >= 100000:                   # plain amount, e.g. Rs. 4,50,00,000 or USD 58,900,976
        rupees_cr = v / 1e7
    else:
        return None                                  # "Rs. 52" without a unit: too ambiguous
    return round(rupees_cr * fx, 2)


def order_value_cr(text: str) -> tuple[float | None, str]:
    """(amount in ₹ crore, note). note: "" | "fx" (converted) | "band" (lower bound of a size band).

    Picks the amount that reads like the order value: an "aggregating to" total first,
    then the first amount near order/contract/worth words.
    """
    text = re.sub(r"\s+", " ", text or "")
    # "10,99,79,910 (Rupees Ten Crore ...)" -> "Rs 10,99,79,910"
    text = re.sub(_NUM + r"\s*/?-?\s*\(\s*rupees", r"Rs \1 (rupees", text, flags=re.I)
    found = []
    for m in _MONEY.finditer(text):
        val = _to_crore(m.group("cur"), m.group(2), m.group("unit"))
        if not val or val < 0.1:
            continue
        before = text[max(0, m.start() - 120):m.start()]
        near = text[max(0, m.start() - 60):m.end() + 25]
        if _CONTEXT_BAD.search(near):
            continue
        score = 3 if _CONTEXT_TOTAL.search(text[max(0, m.start() - 80):m.start()]) else \
            2 if _CONTEXT_GOOD.search(before) else 1
        cur = m.group("cur").lower().replace(" ", "").rstrip(".")
        found.append((score, m, val, "" if cur in ("₹", "rs", "inr", "rupees") else "fx"))
    if found:
        top = max(f[0] for f in found)
        score, m, val, note = next(f for f in found if f[0] == top)   # first well-placed amount
        if note == "fx":       # "USD 52 mn equivalent to ~ INR 492 Crores": use the company's own INR figure
            nxt = next((f for f in found if 0 <= f[1].start() - m.end() <= 80 and not f[3]), None)
            if nxt:
                return nxt[2], ""
        return val, note
    if re.search(r"order classification|classification of (the )?order", text, re.I):
        for word, low in _BANDS:
            if re.search(rf"\b(bags?|bagged|wins?|won|secures?|secured|receives?|received)\b[^.]{{0,20}}?\b{word}\b"
                         rf"[^.]{{0,15}}?\b(orders?|contracts?)|\b{word}\b\W{{0,3}}(orders?|contracts?)", text, re.I):
                return float(low), "band"
    return None, ""


def pdf_text(path: Path | None, pages: int = 3) -> str:
    if not path or path.suffix.lower() != ".pdf":
        return ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return " ".join((p.extract_text() or "") for p in reader.pages[:pages])
    except Exception:
        return ""


# ── revenue: NSE ─────────────────────────────────────────────────────────────
def _quarter_revenue_from_xbrl(xml_bytes: bytes) -> tuple[str, float] | None:
    """(quarter end yyyy-mm-dd, revenue ₹ crore) for the 3-month period in an integrated-filing XBRL."""
    root = ET.fromstring(xml_bytes)
    ctx = {}
    for c in root.iter():
        if c.tag.endswith("}context"):
            if any(e.tag.endswith("explicitMember") for e in c.iter()):
                continue                              # segment / dimension facts: skip
            st = next((e.text for e in c.iter() if e.tag.endswith("startDate")), None)
            en = next((e.text for e in c.iter() if e.tag.endswith("endDate")), None)
            if st and en:
                ctx[c.get("id")] = (st, en)
    facts = {}
    for e in root.iter():
        name = e.tag.split("}")[-1]
        if name in ("RevenueFromOperations", "Income", "TotalIncome", "InterestEarned") and e.text:
            p = ctx.get(e.get("contextRef"))
            if not p:
                continue
            days = (datetime.fromisoformat(p[1]) - datetime.fromisoformat(p[0])).days
            if 80 <= days <= 100:
                facts.setdefault(name, (p[1], float(e.text) / 1e7))
    for name in ("RevenueFromOperations", "InterestEarned", "TotalIncome", "Income"):
        if name in facts:
            return facts[name]
    return None


def nse_revenue(symbol: str, session) -> dict | None:
    """{'quarter_cr', 'quarter_end', 'annual_cr', 'quarters', 'source'} from NSE, or None."""
    r = session.get(NSE_RESULTS, params={"index": "equities", "symbol": symbol}, timeout=30)
    r.raise_for_status()
    rows = [x for x in r.json().get("data", []) if x.get("type", "").startswith("Integrated Filing- Fin")
            and x.get("xbrl") and x.get("qe_Date") and x.get("consolidated")]
    by_q: dict[str, dict] = {}
    for x in rows:                                    # one filing per quarter, consolidated preferred
        q = x["qe_Date"]
        if q not in by_q or (x["consolidated"] == "Consolidated" and by_q[q]["consolidated"] != "Consolidated"):
            by_q[q] = x
    quarters = sorted(by_q, key=lambda q: datetime.strptime(q, "%d-%b-%Y"), reverse=True)[:4]
    vals = []
    for q in quarters:
        try:
            got = _quarter_revenue_from_xbrl(session.get(by_q[q]["xbrl"], timeout=30).content)
        except Exception:
            got = None
        if got:
            vals.append(got)
    if not vals:
        return None
    vals.sort(reverse=True)
    out = {"quarter_end": vals[0][0], "quarter_cr": round(vals[0][1], 1), "quarters": len(vals),
           "source": f"NSE {by_q[quarters[0]]['consolidated'].lower()}"}
    if len(vals) == 4:
        out["annual_cr"] = round(sum(v for _, v in vals), 1)
    elif len(vals) >= 2:                              # newly listed / gaps: annualise what NSE has
        out["annual_cr"] = round(sum(v for _, v in vals) / len(vals) * 4, 1)
        out["annualised"] = True
    return out


# ── revenue: revenue.csv backup ──────────────────────────────────────────────
def _norm(s: str) -> str:
    s = re.sub(r"\b(limited|ltd|the)\b|[^a-z0-9]", "", (s or "").lower())
    return s


def load_revenue_csv() -> dict:
    """Keys: NSE symbol, BSE code and normalised name -> {'annual_cr', 'quarter_cr'}.

    Accepts Screener / Investkaro exports: a symbol or name column, plus columns such as
    'Sales' / 'Revenue' / 'Sales TTM' (annual, ₹ crore) and 'Sales latest quarter' / 'Quarterly sales'.
    """
    if not REVENUE_CSV.exists():
        return {}
    text = REVENUE_CSV.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return {}
    cols = {c: (c or "").strip().lower() for c in rows[0].keys()}

    def pick(test):
        return next((c for c, l in cols.items() if test(l)), None)

    sym = pick(lambda l: l in ("nse code", "nse_symbol", "nse symbol", "symbol", "nse"))
    bse = pick(lambda l: l in ("bse code", "bse_code", "bse", "scrip code"))
    name = pick(lambda l: l in ("name", "company name", "company"))
    qtr = pick(lambda l: ("quarter" in l or "qtr" in l) and ("sales" in l or "revenue" in l))
    ann = pick(lambda l: l in ("sales", "revenue", "sales ttm", "revenue ttm", "annual_revenue_cr",
                               "revenue_annual_cr", "sales (cr)", "annual sales", "sales annual"))

    def num(v):
        try:
            return float(str(v).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    out = {}
    for r in rows:
        rec = {"annual_cr": num(r.get(ann)) if ann else None, "quarter_cr": num(r.get(qtr)) if qtr else None}
        if not rec["annual_cr"] and not rec["quarter_cr"]:
            continue
        for key in ((r.get(sym) or "").strip().upper() if sym else "",
                    (r.get(bse) or "").strip() if bse else "",
                    _norm(r.get(name)) if name else ""):
            if key:
                out.setdefault(key, rec)
    return out


def company_revenue(item: dict, session, cache: dict, csv_rev: dict) -> dict | None:
    """Revenue for the filing's company: NSE first (cached for 7 days), then revenue.csv."""
    code = item.get("code", "")
    key = code.upper()
    today = datetime.now().strftime("%Y-%m-%d")
    hit = cache.get(key)
    if hit and hit.get("fetched") and (datetime.now() - datetime.fromisoformat(hit["fetched"])).days < 7:
        return hit
    rev = None
    if item.get("exchange") == "NSE" and session is not None:
        try:
            rev = nse_revenue(code, session)
        except Exception:
            rev = None
    if not rev or not rev.get("annual_cr"):
        b = csv_rev.get(key) or csv_rev.get(code) or csv_rev.get(_norm(item.get("stock", "")))
        if b:
            rev = dict(rev or {})
            rev["source"] = (rev.get("source", "") + " + revenue.csv").strip(" +")
            rev["annual_cr"] = rev.get("annual_cr") or b["annual_cr"]
            rev["quarter_cr"] = rev.get("quarter_cr") or b["quarter_cr"]
    if rev:
        rev["fetched"] = today
        cache[key] = rev
    return rev


def order_info(item: dict, pdf: Path | None, session, cache: dict, csv_rev: dict) -> dict:
    text = f"{item.get('subject', '')}. {item.get('detail', '')}"
    value, note = order_value_cr(text)
    if value is None or note == "band":
        v2, n2 = order_value_cr(pdf_text(pdf) + " " + text)
        if v2 is not None:
            value, note = v2, n2
    info = {"value_cr": value, "note": note}
    rev = company_revenue(item, session, cache, csv_rev) or {}
    info.update({k: rev.get(k) for k in ("annual_cr", "quarter_cr", "quarter_end", "source", "annualised")})
    if not info.get("annual_cr") and info.get("quarter_cr"):   # new listing: only 1 quarter filed so far
        info["annual_cr"] = round(4 * info["quarter_cr"], 1)
        info["annualised"] = True
    if value and info.get("annual_cr"):
        info["pct_annual"] = round(100 * value / info["annual_cr"], 1)
    if value and rev.get("quarter_cr"):
        info["pct_quarter"] = round(100 * value / rev["quarter_cr"], 1)
    return info
