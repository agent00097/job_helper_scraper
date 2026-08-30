#!/usr/bin/env python3
"""
Seed SuccessFactors RMK career boards into source_companies.

company_endpoint is the board origin (https://jobs.sap.com), stored under
source_endpoints.successfactors. Existing companies are merged on
normalized_name so SAP (already inserted) is not duplicated.

By default each origin is probed with GET {origin}/tile-search-results/?startrow=0
and skipped unless the response contains RMK job tiles.

Usage (from job-helper-scraper, with DATABASE_URL / DB_*):

  python scripts/seed_successfactors_companies.py --dry-run
  python scripts/seed_successfactors_companies.py
  python scripts/seed_successfactors_companies.py --skip-probe
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_db_connection
from utils.company_normalization import normalize_company_name

SOURCE_NAME = "successfactors"
TILE_PATH = "/tile-search-results/"
PROBE_TIMEOUT = 15
PROBE_WORKERS = 12
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (compatible; HarcoJobScraper/1.0; +https://harco.app)",
}

# (display name, candidate origins). First origin that returns RMK tiles wins.
COMPANIES: list[tuple[str, list[str]]] = [
    ("L3Harris", ["https://jobs.l3harris.com"]),
    ("Cintas", ["https://careers.cintas.com"]),
    ("GXO", ["https://jobs.gxo.com"]),
    ("Kiewit", ["https://kiewitcareers.kiewit.com"]),
    ("Northeast Grocery", ["https://careers.northeastgrocery.com"]),
    ("Corning", ["https://corningjobs.corning.com"]),
    ("Black & Veatch", ["https://careers.bv.com"]),
    ("QuikTrip", ["https://careers.quiktrip.com"]),
    (
        "HII Technical Solutions",
        [
            "https://jobs.hii-tsd.com",
            "https://jobs.hiitsd.com",
            "https://jobs.hii.com",
        ],
    ),
    ("ASSA ABLOY", ["https://assaabloy.jobs2web.com"]),
    ("Hubbell", ["https://careers.hubbell.com"]),
    ("Ingles", ["https://jobs.inglescareers.com"]),
    ("JE Dunn", ["https://jobs.jedunn.com"]),
    ("Churchill Downs", ["https://jobs.churchilldowns.com"]),
    ("Boston Scientific", ["https://jobs.bostonscientific.com"]),
    ("Miami-Dade County Public Schools", ["https://apply.dadeschools.net"]),
    ("Crocs", ["https://careers.crocs.com"]),
    ("Arthrex", ["https://careers.arthrex.com"]),
    ("Grainger", ["https://jobs.grainger.com"]),
    ("Commercial Metals Company", ["https://jobs.cmc.com"]),
    ("Teradyne", ["https://jobs.teradyne.com"]),
    ("WillScot", ["https://careers.willscot.com"]),
    ("Paramount", ["https://careers.paramount.com"]),
    ("Garney Construction", ["https://careers.garney.com"]),
    ("BWXT", ["https://careers.bwxt.com"]),
    ("Mohawk Industries", ["https://careers.mohawkind.com"]),
    ("State of Illinois", ["https://illinois.jobs2web.com"]),
    ("West Pharmaceutical Services", ["https://careers.westpharma.com"]),
    ("Halliburton", ["https://jobs.halliburton.com"]),
    ("SPX", ["https://careers.spx.com"]),
    ("SAP", ["https://jobs.sap.com"]),
    ("PACCAR", ["https://jobs.paccar.com"]),
    ("Flowers Foods", ["https://careers.flowersfoods.com"]),
    ("AGCO", ["https://careers.agcocorp.com"]),
    ("Chobani", ["https://careers.chobani.com"]),
    ("Havertys", ["https://jobs.havertys.com"]),
    ("PG&E", ["https://careers.pge.com"]),
    ("Acuity", ["https://careers.acuityinc.com"]),
    ("Dana", ["https://jobs.dana.com"]),
    ("Barton Malow", ["https://careers.bartonmalow.com"]),
    ("McDonald's", ["https://jobs.mcdonalds.com"]),
    (
        "Provident Bank",
        ["https://careers.provident.bank", "https://careers.providentbank.com"],
    ),
    ("HF Sinclair", ["https://careers.hfsinclair.com"]),
    ("Aptar", ["https://jobs.aptar.com"]),
    ("WEC Energy Group", ["https://careers.wecenergygroup.com"]),
    ("EBSCO Industries", ["https://careers.ebscoind.com"]),
    ("Phillips 66", ["https://careers.phillips66.com"]),
    ("Seneca Foods", ["https://careers.senecafoods.com"]),
    ("Dominion Energy", ["https://careers.dominionenergy.com"]),
    ("DTE Energy", ["https://careers.dteenergy.com"]),
    ("Gates", ["https://careers.gates.com"]),
    ("Element Solutions", ["https://careers.elementsolutionsinc.com"]),
    ("BNSF", ["https://bnsf.jobs2web.com"]),
    ("Kennametal", ["https://jobs.kennametal.com"]),
    ("Matthews International", ["https://careers.matw.com"]),
    ("Hilmar Cheese", ["https://jobs.hilmarcheese.com"]),
    ("Patrick Industries", ["https://careers.patrickind.com"]),
    ("Schaeffler", ["https://jobs.schaeffler.com"]),
    ("ZF", ["https://jobs.zf.com"]),
    ("Hensoldt", ["https://jobs.hensoldt.net"]),
    ("KNDS", ["https://jobs.knds.de"]),
    ("Liebherr", ["https://careers.liebherr.com"]),
    ("MTU", ["https://jobs.mtu.de"]),
]


def _tile_count(html: str) -> int:
    return html.count('class="job-tile')


def probe_origin(origin: str) -> tuple[str, int, str]:
    url = origin.rstrip("/") + TILE_PATH
    try:
        response = requests.get(
            url,
            params={"startrow": 0},
            headers={**_HEADERS, "Referer": origin.rstrip("/") + "/search/"},
            timeout=PROBE_TIMEOUT,
        )
        if response.status_code >= 400:
            return origin, 0, f"http {response.status_code}"
        return origin, _tile_count(response.text or ""), f"http {response.status_code}"
    except requests.exceptions.RequestException as exc:
        return origin, 0, str(exc)


def resolve_company(name: str, origins: list[str], skip_probe: bool) -> tuple[str, str] | None:
    if skip_probe:
        return name, origins[0]
    for origin in origins:
        origin, tiles, detail = probe_origin(origin)
        print(f"  probe {origin} -> {tiles} tiles ({detail})")
        if tiles > 0:
            return name, origin
    return None


def upsert_companies(rows: list[tuple[str, str]], dry_run: bool) -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job_sources (name, type, enabled, schedule_hours, rate_limit_per_minute, config)
                VALUES (%s, 'api', TRUE, 12, 60, '{"max_detail_fetches_per_run": 50, "detail_workers": 4}'::jsonb)
                ON CONFLICT (name) DO UPDATE SET
                    type = EXCLUDED.type,
                    enabled = TRUE,
                    schedule_hours = EXCLUDED.schedule_hours,
                    rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
                    config = EXCLUDED.config,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id
                """,
                (SOURCE_NAME,),
            )
            source_row = cur.fetchone()
            print(f"job_sources {SOURCE_NAME}: {source_row[0] if source_row else 'ok'}")

            inserted = 0
            for company_name, origin in rows:
                normalized = normalize_company_name(company_name)
                sql = """
                    INSERT INTO source_companies
                        (company_name, normalized_name, source_endpoints, enabled)
                    VALUES (%s, %s, jsonb_build_object(%s::text, %s::text), TRUE)
                    ON CONFLICT (normalized_name) DO UPDATE SET
                        source_endpoints = source_companies.source_endpoints
                                           || jsonb_build_object(%s::text, %s::text),
                        updated_at = NOW()
                """
                params = (
                    company_name,
                    normalized,
                    SOURCE_NAME,
                    origin,
                    SOURCE_NAME,
                    origin,
                )
                print(f"  upsert {company_name} ({normalized}) -> {origin}")
                if not dry_run:
                    cur.execute(sql, params)
                    inserted += 1
            if dry_run:
                conn.rollback()
                print(f"dry-run: would upsert {len(rows)} companies")
            else:
                conn.commit()
                print(f"upserted {inserted} companies")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print actions; do not write")
    parser.add_argument(
        "--skip-probe",
        action="store_true",
        help="Insert the first candidate origin without hitting the live board",
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Probe boards and print winners; do not connect to the database",
    )
    args = parser.parse_args()

    resolved: list[tuple[str, str]] = []
    skipped: list[str] = []
    print(f"Resolving {len(COMPANIES)} SuccessFactors RMK candidates")
    if args.skip_probe:
        for name, origins in COMPANIES:
            resolved.append((name, origins[0]))
    else:
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            futures = {
                pool.submit(resolve_company, name, origins, False): name
                for name, origins in COMPANIES
            }
            for fut in as_completed(futures):
                name = futures[fut]
                result = fut.result()
                if result:
                    resolved.append(result)
                else:
                    skipped.append(name)

    resolved.sort(key=lambda row: row[0].lower())
    print(f"\nReady: {len(resolved)}")
    for name, origin in resolved:
        print(f"  {name}\t{origin}")
    if skipped:
        print(f"\nSkipped (no RMK tiles): {len(skipped)}")
        for name in sorted(skipped):
            print(f"  {name}")

    if args.probe_only:
        return
    upsert_companies(resolved, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
