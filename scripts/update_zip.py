#!/usr/bin/env python3
"""Build ZIP-code housing data for the local housing lookup.

Sources (free, no key needed):
  - Zillow Home Value Index (ZHVI) by ZIP, typical home value, monthly
  - Zillow Observed Rent Index (ZORI) by ZIP, typical rent, monthly
  - US Census American Community Survey 5-year median household income by ZIP (ZCTA)
  - US Census Gazetteer ZIP (ZCTA) centroids, for the nearby-ZIP search

Writes data/zip/<first 3 digits>.json shards, data/zip/near.json (a compact index of every
ZIP's location and scoring inputs for the nearby search) and data/zip/meta.json.
Zillow updates monthly, so by default this skips work if it ran in the last 7 days.

Usage:
  python scripts/update_zip.py [--force] [--zhvi FILE] [--zori FILE] [--income FILE]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import zipfile
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "zip"

ZHVI_URL = "https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv"
ZORI_URL = "https://files.zillowstatic.com/research/public_csvs/zori/Zip_zori_uc_sfrcondomfr_sm_month.csv"
CENSUS_URL = "https://api.census.gov/data/{year}/acs/acs5"
GAZETTEER_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer/{year}_Gaz_zcta_national.zip"
VERSION = 2  # bump to force a rebuild when the output format changes

HISTORY_STEP_MONTHS = 6   # one history point every 6 months
HISTORY_YEARS = 10


def get(url: str, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "investing-barometer/1.0"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001
            last = err
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"GET {url.split('?')[0]} failed: {last}")


def read_source(path: str | None, url: str) -> str:
    if path:
        return Path(path).read_text()
    print(f"Downloading {url.split('/')[-1]} ...")
    return get(url).decode("utf-8")


def zillow_table(text: str) -> tuple[list[str], dict[str, dict]]:
    """Returns (date columns, {zip: {meta..., 'vals': [float|None per date]}})."""
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    date_idx = [i for i, h in enumerate(header) if len(h) == 10 and h[4] == "-" and h[7] == "-"]
    col = {h: i for i, h in enumerate(header)}
    dates = [header[i] for i in date_idx]
    out = {}
    for row in reader:
        if len(row) < len(header):
            continue
        z = row[col["RegionName"]].strip().zfill(5)
        vals = []
        for i in date_idx:
            try:
                vals.append(float(row[i]) if row[i] else None)
            except ValueError:
                vals.append(None)
        out[z] = {
            "city": row[col.get("City", 0)] if "City" in col else "",
            "state": row[col["State"]] if "State" in col else row[col.get("StateName", 0)],
            "metro": row[col["Metro"]] if "Metro" in col else "",
            "county": row[col["CountyName"]] if "CountyName" in col else "",
            "vals": vals,
        }
    return dates, out


def last_valid(vals: list) -> int | None:
    for i in range(len(vals) - 1, -1, -1):
        if vals[i] is not None:
            return i
    return None


def census_income(path: str | None) -> tuple[int | None, dict[str, int]]:
    if path:
        rows = json.loads(Path(path).read_text())
        return 0, {r[1].zfill(5): int(r[0]) for r in rows[1:] if r[0] and int(r[0]) > 0}
    key = os.environ.get("CENSUS_API_KEY")
    this_year = date.today().year
    for year in range(this_year - 1, this_year - 6, -1):
        params = {"get": "B19013_001E", "for": "zip code tabulation area:*"}
        if key:
            params["key"] = key
        url = CENSUS_URL.format(year=year) + "?" + urllib.parse.urlencode(params)
        try:
            raw = get(url, retries=2)
            rows = json.loads(raw)
        except json.JSONDecodeError:
            snippet = raw[:200].decode("utf-8", "replace").replace("\n", " ")
            print(f"  ACS {year}: unexpected response ({len(raw)} bytes): {snippet}")
            continue
        except Exception as err:  # noqa: BLE001 - year not published yet, try older
            print(f"  ACS {year}: unavailable ({str(err)[:120]})")
            continue
        income = {}
        for r in rows[1:]:
            try:
                v = int(float(r[0]))
            except (TypeError, ValueError):
                continue
            if v > 0:
                income[r[-1].zfill(5)] = v
        print(f"  ACS {year}: {len(income)} ZIPs with income")
        return year, income
    return None, {}


def parse_gazetteer(text: str) -> dict[str, tuple[float, float]]:
    lines = text.splitlines()
    header = [h.strip() for h in lines[0].split("\t")]
    gi, la, lo = header.index("GEOID"), header.index("INTPTLAT"), header.index("INTPTLONG")
    out = {}
    for line in lines[1:]:
        parts = [p.strip() for p in line.split("\t")]
        try:
            out[parts[gi].zfill(5)] = (float(parts[la]), float(parts[lo]))
        except (IndexError, ValueError):
            continue
    return out


def zip_coords(path: str | None) -> tuple[int | None, dict[str, tuple[float, float]]]:
    if path:
        return 0, parse_gazetteer(Path(path).read_text())
    this_year = date.today().year
    for year in range(this_year, this_year - 5, -1):
        try:
            raw = get(GAZETTEER_URL.format(year=year), retries=2)
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                name = next(n for n in zf.namelist() if n.endswith(".txt"))
                text = zf.read(name).decode("utf-8", "replace")
        except Exception as err:  # noqa: BLE001 - not published yet, try older
            print(f"  Gazetteer {year}: unavailable ({str(err)[:80]})")
            continue
        coords = parse_gazetteer(text)
        print(f"  Gazetteer {year}: {len(coords)} ZIP centroids")
        return year, coords
    return None, {}


def build(zhvi_text: str, zori_text: str | None, income_year, income: dict,
          coords: dict | None = None, coords_year=None) -> tuple[dict, dict, dict]:
    coords = coords or {}
    near = {"z": [], "la": [], "lo": [], "v": [], "y1": [], "r": [], "i": [], "c": [], "s": []}
    dates, homes = zillow_table(zhvi_text)
    rent_dates, rents = zillow_table(zori_text) if zori_text else ([], {})

    shards: dict[str, dict] = {}
    latest_date = None
    for z, h in homes.items():
        vals = h["vals"]
        li = last_valid(vals)
        if li is None:
            continue
        v = vals[li]
        latest_date = max(latest_date or dates[li], dates[li])
        y1 = vals[li - 12] if li >= 12 else None
        y5 = vals[li - 60] if li >= 60 else None
        hist = []
        for k in range(HISTORY_YEARS * 12 // HISTORY_STEP_MONTHS, -1, -1):
            j = li - k * HISTORY_STEP_MONTHS
            if j >= 0 and vals[j] is not None:
                hist.append([dates[j][:7], round(vals[j])])

        rec = {
            "c": h["city"], "s": h["state"], "m": h["metro"], "co": h["county"],
            "v": round(v), "d": dates[li][:7],
            "y1": round((v / y1 - 1) * 100, 1) if y1 else None,
            "y5": round(((v / y5) ** (1 / 5) - 1) * 100, 1) if y5 else None,
            "h": hist,
        }
        r = rents.get(z)
        if r:
            ri = last_valid(r["vals"])
            if ri is not None:
                rec["r"] = round(r["vals"][ri])
                rec["rd"] = rent_dates[ri][:7]
                r1 = r["vals"][ri - 12] if ri >= 12 else None
                rec["ry1"] = round((r["vals"][ri] / r1 - 1) * 100, 1) if r1 else None
        if z in income:
            rec["i"] = income[z]
        if z in coords:
            rec["la"], rec["lo"] = round(coords[z][0], 4), round(coords[z][1], 4)
            for k, val in (("z", z), ("la", rec["la"]), ("lo", rec["lo"]), ("v", rec["v"]), ("y1", rec["y1"]),
                           ("r", rec.get("r")), ("i", rec.get("i")), ("c", rec["c"]), ("s", rec["s"])):
                near[k].append(val)
        shards.setdefault(z[:3], {})[z] = rec

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zhvi_as_of": latest_date[:7] if latest_date else None,
        "zori_as_of": max((rent_dates[last_valid(r["vals"])][:7] for r in rents.values() if last_valid(r["vals"]) is not None), default=None),
        "income_year": income_year,
        "coords_year": coords_year,
        "located_count": len(near["z"]),
        "version": VERSION,
        "zip_count": sum(len(s) for s in shards.values()),
        "sources": {
            "home_values": "Zillow Home Value Index (ZHVI), all homes, middle tier, smoothed & seasonally adjusted",
            "rents": "Zillow Observed Rent Index (ZORI), all homes",
            "income": "US Census Bureau, American Community Survey 5-year estimates (B19013)",
            "locations": "US Census Bureau Gazetteer, ZIP Code Tabulation Area centroids",
        },
    }
    return shards, meta, near


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--zhvi")
    ap.add_argument("--zori")
    ap.add_argument("--income")
    ap.add_argument("--gazetteer")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    meta_path = args.out / "meta.json"
    if meta_path.exists() and not args.force:
        prev = json.loads(meta_path.read_text())
        age = datetime.now(timezone.utc) - datetime.fromisoformat(prev["generated_at"].replace("Z", "+00:00"))
        missing_income = prev.get("income_year") is None and os.environ.get("CENSUS_API_KEY")
        if age < timedelta(days=7) and not missing_income and prev.get("version") == VERSION:
            print(f"ZIP data is {age.days} days old; skipping (use --force to rebuild).")
            return 0

    zhvi = read_source(args.zhvi, ZHVI_URL)
    try:
        zori = read_source(args.zori, ZORI_URL)
    except Exception as err:  # noqa: BLE001 - rents are optional
        print(f"Rent data unavailable: {err}", file=sys.stderr)
        zori = None
    try:
        income_year, income = census_income(args.income)
    except Exception as err:  # noqa: BLE001 - income is optional
        print(f"Income data unavailable: {err}", file=sys.stderr)
        income_year, income = None, {}

    try:
        coords_year, coords = zip_coords(args.gazetteer)
    except Exception as err:  # noqa: BLE001 - locations are optional
        print(f"ZIP locations unavailable: {err}", file=sys.stderr)
        coords_year, coords = None, {}

    shards, meta, near = build(zhvi, zori, income_year, income, coords, coords_year)
    if meta["zip_count"] < 1000 and not args.zhvi:
        print(f"Only {meta['zip_count']} ZIPs parsed; refusing to overwrite.", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("[0-9][0-9][0-9].json"):
        if old.stem not in shards:
            old.unlink()
    for prefix, data in shards.items():
        (args.out / f"{prefix}.json").write_text(json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n")
    if near["z"]:
        (args.out / "near.json").write_text(json.dumps(near, separators=(",", ":")) + "\n")
    meta_path.write_text(json.dumps(meta, indent=1) + "\n")
    print(f"Wrote {meta['zip_count']} ZIPs in {len(shards)} files. Home values as of {meta['zhvi_as_of']}, "
          f"rents {meta['zori_as_of']}, income ACS {meta['income_year']}, "
          f"{meta['located_count']} located (Gazetteer {meta['coords_year']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
