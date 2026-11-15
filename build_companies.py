"""
Build docs/companies.json: the company database the watchlist picker searches.

It merges three sources on ISIN:
  1. NSE's official equity list (downloaded): NSE symbol, name, ISIN
  2. BSE's active equity list (downloaded): BSE code, name, ISIN, market cap
  3. Your Industry Mapping CSV (local): industry, short name, market cap

Run it on your PC (BSE blocks GitHub's servers), then upload docs/companies.json:
    python build_companies.py
    python build_companies.py --mapping "path\\to\\Industry Mapping.csv"
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path

import requests

from watchlist_alerts import BSE_HEADERS, IST, UA

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "companies.json"
DEFAULT_MAPPING = Path(r"H:\AWork\Ext\WIP\Data Collection_Analytics\Industry Mapping\Industry Mapping.csv")
NSE_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_LIST = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    return re.sub(r"\s*\b(Limited|Ltd\.?)$", "", name, flags=re.I).strip()


def num(v) -> float:
    try:
        return round(float(str(v).replace(",", "")), 1)
    except (TypeError, ValueError):
        return 0.0


def load_nse() -> list[dict]:
    r = requests.get(NSE_LIST, headers={"User-Agent": UA, "Accept": "*/*"}, timeout=60)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    rows = [{k.strip(): (v or "").strip() for k, v in x.items() if k} for x in rows]
    print(f"NSE list: {len(rows)} companies")
    return rows


def load_bse() -> list[dict]:
    s = requests.Session()
    s.headers.update(BSE_HEADERS)
    r = s.get(BSE_LIST, params={"Group": "", "Scripcode": "", "industry": "",
                                "segment": "Equity", "status": "Active"}, timeout=90)
    r.raise_for_status()
    rows = r.json()
    print(f"BSE list: {len(rows)} companies")
    return rows


def load_mapping(path: Path) -> list[dict]:
    if not path.exists():
        print(f"Industry mapping not found ({path}); industries will be blank")
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"Industry mapping: {len(rows)} companies")
    return rows


def build(mapping_path: Path) -> list[list]:
    nse, bse, mapping = load_nse(), load_bse(), load_mapping(mapping_path)
    by_bse = {m["BSE Code"].strip(): m for m in mapping if m.get("BSE Code", "").strip()}
    by_nse = {m["NSE Code"].strip().upper(): m for m in mapping if m.get("NSE Code", "").strip()}

    cos: dict[str, dict] = {}          # ISIN -> company
    for x in nse:
        isin = x.get("ISIN NUMBER", "")
        if not isin:
            continue
        cos[isin] = {"name": clean_name(x.get("NAME OF COMPANY")), "nse": x["SYMBOL"].upper(),
                     "bse": "", "isin": isin, "ind": "", "mcap": 0.0, "alt": ""}
    for x in bse:
        isin = (x.get("ISIN_NUMBER") or "").strip()
        code = str(x.get("SCRIP_CD") or "").strip()
        if not code or not isin.startswith("IN"):
            continue
        c = cos.setdefault(isin, {"name": clean_name(x.get("Issuer_Name") or x.get("Scrip_Name")),
                                  "nse": "", "bse": "", "isin": isin, "ind": "", "mcap": 0.0, "alt": ""})
        c["bse"] = code
        c["mcap"] = num(x.get("Mktcap"))
        if not c["alt"]:
            c["alt"] = (x.get("scrip_id") or "").strip()

    for c in cos.values():
        m = by_bse.get(c["bse"]) or by_nse.get(c["nse"])
        if m:
            c["ind"] = (m.get("Industry") or "").strip()
            short = (m.get("Name") or "").strip()
            if short and short.lower() != c["name"].lower():
                c["alt"] = f"{c['alt']} {short}".strip()
            if not c["mcap"]:
                c["mcap"] = num(m.get("Market Capitalization"))

    rows = [[c["name"], c["nse"], c["bse"], c["isin"], c["ind"], c["mcap"], c["alt"]]
            for c in cos.values() if c["name"]]
    rows.sort(key=lambda r: -r[5])
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING, help="Industry Mapping CSV")
    args = ap.parse_args()
    rows = build(args.mapping)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = {"updated": f"{datetime.now(IST):%Y-%m-%d}",
            "fields": ["name", "nse", "bse", "isin", "industry", "mcap_cr", "alt"],
            "rows": rows}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    both = sum(1 for r in rows if r[1] and r[2])
    print(f"Saved {len(rows)} companies to {OUT} "
          f"({both} on both, {sum(1 for r in rows if r[1] and not r[2])} NSE only, "
          f"{sum(1 for r in rows if r[2] and not r[1])} BSE only, "
          f"{sum(1 for r in rows if r[4])} with industry), {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
