#!/usr/bin/env python3
"""
Build a reviewed plan to fix every failing (company, source) on Scraper health.

Inputs are the two JSON exports taken from the admin portal:
  * companies.json  every company: name, sources ("ashby:slug", "workday:URL"),
                    domain, status, last scrape status (Companies page)
  * health.json     {"failing": [...], "zero": [...]} from Scraper health

Output:
  * plan.csv        one row per failing or empty (company, source) and per
                    duplicate company row, with an action and a confidence
  * apply_plan.sql  how to apply the high-confidence actions (run by whoever
                    owns the database, after reviewing plan.csv)

This script never writes to the database.

Usage:
    python scripts/reconcile_company_sources.py companies.json health.json
    python scripts/reconcile_company_sources.py companies.json health.json --zero   # also probe empty boards
    python scripts/reconcile_company_sources.py companies.json health.json --offline # no network

Actions:
  wait             failure is temporary or fixed in code (PR "fix company scrape
                   errors"); no data change
  remove_source    drop this source from the company: it is dead/empty and the
                   company has another source that works, or it moved to an ATS
                   we don't scrape while other sources remain
  relocate         replace this source's endpoint with the board the company
                   uses now (same or different ATS)
  disable_company  turn the company off: junk/test board, duplicate row, or the
                   only source is dead and no new board was found
  review           needs a human (blocked by the site, ambiguous match)
  keep             nothing to do (e.g. empty board with no better alternative)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import requests  # noqa: E402

import resolve_dead_boards as rdb  # noqa: E402

PLAN_COLS = [
    "action", "confidence", "company_name", "source", "endpoint",
    "new_source", "new_endpoint", "category", "reason", "evidence", "other_sources",
]

SUPPORTED_SOURCES = {"ashby", "greenhouse", "lever", "smartrecruiters", "workday", "successfactors"}
_SHARED_API_HOSTS = (
    "boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co",
    "api.ashbyhq.com", "api.smartrecruiters.com",
)


# --------------------------------------------------------------------------
# Failure classification (from the Scraper health "reason" text)
# --------------------------------------------------------------------------
def classify_failure(source: str, reason: str) -> tuple[str, str]:
    """
    Return (category, short label).

      transient    network blip or our own capacity issue; retried in code
      code_fixed   caused by a scraper bug fixed in the code PR
      external     the company's own site problem (expired SSL)
      board_gone   the board/endpoint no longer exists or is wrong
      blocked      the site refuses us (403)
    """
    r = reason or ""
    status_m = re.search(r"\b(\d{3}) (?:Client|Server) Error", r)
    status = int(status_m.group(1)) if status_m else None

    if "couldn't get a connection" in r:
        return "transient", "scraper DB pool exhausted (our side)"
    if re.search(r"exceeded \d+s company scrape timeout", r):
        return "code_fixed", "company scrape timeout (detail-fetch cap)"
    if "duplicate key value" in r or "index row size" in r:
        return "code_fixed", "database insert error"
    if "certificate" in r.lower():
        return "external", "expired/invalid SSL certificate"
    if status in (429, 500, 502, 503, 504) or "Read timed out" in r or "Connection aborted" in r \
            or "RemoteDisconnected" in r or "ConnectTimeout" in r:
        return "transient", f"temporary error ({status or 'timeout'})"
    if "NameResolution" in r or "Failed to resolve" in r:
        host_m = re.search(r"host='([^']+)'", r)
        host = host_m.group(1) if host_m else ""
        if host in _SHARED_API_HOSTS or "startrow=" in r and "startrow=0" not in r:
            return "transient", "DNS blip"
        return "board_gone", f"host does not resolve ({host})"
    if source == "workday" and status in (404, 405) and re.search(r"/wday/cxs/[^/]+/[a-z]{2}(?:-[A-Za-z]{2})?(?:/|/jobs)", r):
        return "code_fixed", "language code in Workday URL"
    if source == "successfactors" and status == 404 and re.search(r"\.com/[a-z]{2}(?:_[A-Z]{2})?/tile-search", r):
        return "code_fixed", "language code in SuccessFactors URL"
    if "has no site name" in r:
        return "board_gone", "Workday URL has only a language code"
    if status == 403:
        return "blocked", "403 Forbidden"
    if status in (400, 404, 405, 410, 422):
        return "board_gone", f"HTTP {status}"
    return "transient", "unclassified; re-check after code fix"


# --------------------------------------------------------------------------
# Workday-specific relocation: other data centers, current site path
# --------------------------------------------------------------------------
WORKDAY_SHARDS = (
    ["wd1", "wd2", "wd3", "wd4", "wd5", "wd10", "wd12"]
    + [f"wd{n}" for n in range(100, 113)]
)
_COMMON_SITES = ("External", "Careers", "careers", "External_Careers", "External_Career_Site", "Jobs")
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:[-_][A-Za-z]{2})?$")


def _workday_parts(url: str) -> Optional[tuple[str, str, str]]:
    p = urlparse(url if "://" in url else "https://" + url)
    labels = (p.hostname or "").split(".")
    if len(labels) < 4 or labels[2] != "myworkdayjobs":
        return None
    segs = [s for s in (p.path or "").split("/") if s]
    while segs and _LOCALE_RE.match(segs[0]):
        segs.pop(0)
    site = segs[0] if segs and segs[0].lower() not in {"job", "jobs", "wday"} else ""
    return labels[0], labels[1], site


def _workday_total(tenant: str, shard: str, site: str) -> Optional[int]:
    """Job count if this tenant/shard/site is a live board, else None.
    Raises ConnectionError when the host itself does not exist."""
    url = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    r = rdb.SESSION.post(
        url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
        timeout=rdb.TIMEOUT, headers={"Accept": "application/json"},
    )
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return int(data.get("total") or 0) if isinstance(data, dict) and "jobPostings" in data else None


def _workday_default_site(tenant: str, shard: str) -> Optional[str]:
    """The host root usually redirects to the tenant's default career site."""
    try:
        r = rdb.SESSION.get(f"https://{tenant}.{shard}.myworkdayjobs.com/",
                            timeout=rdb.TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    parts = _workday_parts(r.url or "")
    return parts[2] if parts and parts[2] else None


def probe_workday(endpoint: str) -> Optional[rdb.Hit]:
    parts = _workday_parts(endpoint)
    if not parts:
        return None
    tenant, shard, site = parts
    shards = [shard] + [s for s in WORKDAY_SHARDS if s != shard]
    live_hosts: list[str] = []
    for sh in shards:
        try:
            total = _workday_total(tenant, sh, site) if site else None
        except requests.RequestException:
            continue  # host does not exist on this data center
        live_hosts.append(sh)
        if total is not None:
            note = "same site" if sh == shard else f"moved from {shard} to {sh}"
            return rdb.Hit("workday", f"https://{tenant}.{sh}.myworkdayjobs.com/{site}",
                           total, evidence=note, confidence="high")
    # The host exists but the site path changed (HTTP 422/404): find the current one.
    for sh in live_hosts[:3]:
        candidates = []
        default = _workday_default_site(tenant, sh)
        if default:
            candidates.append(default)
        candidates += [s for s in _COMMON_SITES + (tenant, f"{tenant}_careers") if s not in candidates]
        for cand in candidates:
            if cand == site and sh == shard:
                continue
            try:
                total = _workday_total(tenant, sh, cand)
            except requests.RequestException:
                break
            if total is not None:
                how = "default site" if cand == default else "common site name"
                return rdb.Hit("workday", f"https://{tenant}.{sh}.myworkdayjobs.com/{cand}",
                               total, evidence=f"site changed from '{site}' to '{cand}' ({how})",
                               confidence="high" if cand == default else "medium")
    return None


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
@dataclass
class Company:
    name: str
    sources: dict[str, str]   # source -> endpoint
    domain: Optional[str]
    enabled: bool
    scrape: str


def load_companies(path: str) -> dict[str, list[Company]]:
    by_name: dict[str, list[Company]] = defaultdict(list)
    for r in json.load(open(path, encoding="utf-8")):
        sources = {}
        for s in r.get("s") or []:
            src, _, ep = s.partition(":")
            if src and ep:
                sources[src] = ep
        d = (r.get("d") or "").strip()
        by_name[r["n"]].append(Company(
            name=r["n"], sources=sources,
            domain=None if d in ("", "—", "-") else d,
            enabled=(r.get("st") or "").lower().startswith("enabled"),
            scrape=r.get("sc") or "",
        ))
    return by_name


def endpoint_key(source: str, endpoint: str) -> str:
    ep = endpoint.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
    if source == "workday":
        parts = _workday_parts(endpoint)
        if parts:
            ep = f"{parts[0]}/{parts[2].lower()}"  # tenant + site, any data center
    return f"{source}:{ep}"


def same_board(source: str, endpoint: str, hit: "rdb.Hit") -> bool:
    """True when the 'new' board is the one we already have (same data center too)."""
    if hit.source != source:
        return False
    if source == "workday":
        a, b = _workday_parts(endpoint), _workday_parts(hit.endpoint)
        return bool(a and b) and (a[0], a[1], a[2].lower()) == (b[0], b[1], b[2].lower())
    return endpoint.strip().lower() == hit.endpoint.strip().lower()


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def _row(action, confidence, company, source, endpoint, category, reason,
         evidence="", new_source="", new_endpoint="", other=None) -> dict:
    return {
        "action": action, "confidence": confidence, "company_name": company,
        "source": source, "endpoint": endpoint, "new_source": new_source,
        "new_endpoint": new_endpoint, "category": category, "reason": reason,
        "evidence": evidence,
        "other_sources": "; ".join(f"{s}:{e}" for s, e in (other or {}).items()),
    }


def find_relocation(company: Company, source: str, endpoint: str) -> tuple[Optional[rdb.Hit], dict]:
    """Network search for the company's current board. Returns (hit, resolver result)."""
    hit = probe_workday(endpoint) if source == "workday" else None
    if hit and hit.confidence == "high":
        return hit, {}
    res = rdb.resolve_one({
        "company_name": company.name, "source": source,
        "old_endpoint": endpoint if source != "workday" else (_workday_parts(endpoint) or ("", "", ""))[0],
        "domain": company.domain or "",
    })
    if res.get("status") == "relocated":
        other = rdb.Hit(res["new_source"], res["new_endpoint"],
                        int(res["jobs"]) if res.get("jobs") else None,
                        evidence=res.get("evidence", ""), confidence=res.get("confidence", "low"))
        hit = rdb._better(hit, other)
    return hit, res


def plan_failure(company: Company, source: str, endpoint: str, reason: str,
                 failing_keys: set, zero_keys: set, ep_owner: dict, offline: bool) -> dict:
    category, label = classify_failure(source, reason)
    other = {s: e for s, e in company.sources.items() if s != source}
    healthy_other = {s: e for s, e in other.items()
                     if (company.name, s) not in failing_keys and (company.name, s) not in zero_keys}

    if category in ("transient", "code_fixed", "external"):
        return _row("wait", "high", company.name, source, endpoint, category, label, other=other)

    if rdb.is_junk(company.name, endpoint):
        return _row("disable_company", "high", company.name, source, endpoint, category, label,
                    evidence="test/sandbox/internal/referral board", other=other)

    if healthy_other:
        return _row("remove_source", "high", company.name, source, endpoint, category, label,
                    evidence=f"company already scrapes fine on {', '.join(sorted(healthy_other))}",
                    other=other)

    if offline:
        return _row("review", "low", company.name, source, endpoint, category, label,
                    evidence="offline run: relocation not searched", other=other)

    hit, res = find_relocation(company, source, endpoint)
    if hit and same_board(source, endpoint, hit):
        return _row("wait", "high", company.name, source, endpoint, category, label,
                    evidence=f"board answers normally now (jobs={hit.jobs}); failure was temporary",
                    other=other)
    if hit:
        owner = ep_owner.get(endpoint_key(hit.source, hit.endpoint))
        if owner and owner != company.name:
            return _row("remove_source", hit.confidence, company.name, source, endpoint, category, label,
                        evidence=f"its new board {hit.source}:{hit.endpoint} is already scraped by "
                                 f"company '{owner}' (duplicate row)", other=other)
        if hit.source in company.sources and company.sources[hit.source].lower() == hit.endpoint.lower():
            return _row("remove_source", "high", company.name, source, endpoint, category, label,
                        evidence=f"new board is its existing {hit.source} source", other=other)
        return _row("relocate", hit.confidence, company.name, source, endpoint, category, label,
                    evidence=f"{hit.evidence} (jobs={hit.jobs})".strip(),
                    new_source=hit.source, new_endpoint=hit.endpoint, other=other)
    if res.get("status") == "unsupported_ats":
        action = "remove_source" if other else "disable_company"
        return _row(action, "high", company.name, source, endpoint, category, label,
                    evidence=f"moved to {res.get('new_source')} (not scraped by Harco)", other=other)
    if category == "blocked":
        return _row("review", "medium", company.name, source, endpoint, category, label,
                    evidence="site refuses requests; may need headers or be disabled", other=other)
    action = "remove_source" if other else "disable_company"
    return _row(action, "medium", company.name, source, endpoint, category, label,
                evidence="no live board found on any supported ATS or the careers page", other=other)


def plan_zero(company: Company, source: str, endpoint: str,
              failing_keys: set, zero_keys: set, ep_owner: dict, offline: bool) -> dict:
    other = {s: e for s, e in company.sources.items() if s != source}
    healthy_other = {s: e for s, e in other.items()
                     if (company.name, s) not in failing_keys and (company.name, s) not in zero_keys}
    label = "0 jobs for 7 days"
    if rdb.is_junk(company.name, endpoint):
        return _row("disable_company", "high", company.name, source, endpoint, "empty_board", label,
                    evidence="test/sandbox/internal/referral board", other=other)
    if healthy_other:
        return _row("remove_source", "high", company.name, source, endpoint, "empty_board", label,
                    evidence=f"empty here; company has jobs on {', '.join(sorted(healthy_other))}",
                    other=other)
    if offline:
        return _row("keep", "low", company.name, source, endpoint, "empty_board", label, other=other)
    hit, _res = find_relocation(company, source, endpoint)
    if hit and (hit.jobs or 0) > 0 and hit.confidence == "high" and \
            endpoint_key(hit.source, hit.endpoint) != endpoint_key(source, endpoint):
        owner = ep_owner.get(endpoint_key(hit.source, hit.endpoint))
        if owner and owner != company.name:
            return _row("disable_company", "medium", company.name, source, endpoint, "empty_board", label,
                        evidence=f"company now hires on {hit.source}:{hit.endpoint}, already scraped by "
                                 f"'{owner}' (duplicate row)", other=other)
        return _row("relocate", "high", company.name, source, endpoint, "empty_board", label,
                    evidence=f"{hit.evidence} (jobs={hit.jobs})".strip(),
                    new_source=hit.source, new_endpoint=hit.endpoint, other=other)
    return _row("keep", "medium", company.name, source, endpoint, "empty_board", label,
                evidence="board exists but is empty; no other board found", other=other)


def _sluggy(name: str) -> bool:
    """Looks like an ATS slug rather than a company name: 'Achieve1',
    'bjakcareer', 'a-place-for-mom', 'Experienced'."""
    n = name.strip()
    return (bool(re.search(r"\d$", n)) or "-" in n or n == n.lower()
            or bool(re.search(r"careers?$|jobs$", n, re.I))
            or n.lower() in {"experienced", "external", "careers", "jobs", "bank"})


def _keeper_score(c: Company) -> tuple:
    """Prefer the row that looks like the real company: enabled, a real name
    (not a slug), a domain (logo/industry), more sources."""
    return (c.enabled, not _sluggy(c.name), bool(c.domain), len(c.sources), " " in c.name, len(c.name))


def plan_duplicates(by_name: dict[str, list[Company]]) -> tuple[list[dict], dict[str, str]]:
    """Rows that scrape the same board. Returns (plan rows, endpoint -> owner name)."""
    owners: dict[str, list[tuple[Company, str, str]]] = defaultdict(list)
    for rows in by_name.values():
        for c in rows:
            for s, e in c.sources.items():
                owners[endpoint_key(s, e)].append((c, s, e))
    plan, ep_owner = [], {}
    for key, entries in owners.items():
        companies = {id(c): c for c, _, _ in entries}
        ranked = sorted(companies.values(), key=_keeper_score, reverse=True)
        ep_owner[key] = ranked[0].name
        if len(ranked) < 2:
            continue
        keeper = ranked[0]
        for c, s, e in entries:
            if c is keeper:
                continue
            others = {k: v for k, v in c.sources.items() if k != s}
            if others:
                plan.append(_row("remove_source", "high", c.name, s, e, "duplicate_row",
                                 "same board scraped by two company rows",
                                 evidence=f"'{keeper.name}' already scrapes {s}:{e}", other=others))
            else:
                plan.append(_row("disable_company", "medium", c.name, s, e, "duplicate_row",
                                 "same board scraped by two company rows",
                                 evidence=f"duplicate of '{keeper.name}'; review and merge jobs",
                                 other=others))
    return plan, ep_owner


APPLY_SQL = r"""-- Apply the HIGH-confidence rows of plan.csv. Review plan.csv first.
-- Matches each row by company_name AND the exact current endpoint, so a row
-- that changed since the export is skipped, not overwritten.
BEGIN;
CREATE TEMP TABLE plan (
  action text, confidence text, company_name text, source text, endpoint text,
  new_source text, new_endpoint text, category text, reason text, evidence text,
  other_sources text
);
\copy plan FROM 'plan.csv' CSV HEADER

-- 1. Drop dead/empty/duplicate sources from companies that keep another source.
UPDATE source_companies sc
SET source_endpoints = sc.source_endpoints - p.source,
    updated_at = CURRENT_TIMESTAMP
FROM plan p
WHERE p.action = 'remove_source' AND p.confidence = 'high'
  AND sc.company_name = p.company_name
  AND sc.source_endpoints->>p.source = p.endpoint
  AND (sc.source_endpoints - p.source) <> '{}'::jsonb
RETURNING sc.company_name, p.source AS removed;

-- 2. Point relocated companies at their new board (skips endpoints another row owns).
UPDATE source_companies sc
SET source_endpoints = (sc.source_endpoints - p.source)
                       || jsonb_build_object(p.new_source::text, p.new_endpoint::text),
    updated_at = CURRENT_TIMESTAMP
FROM plan p
WHERE p.action = 'relocate' AND p.confidence = 'high'
  AND sc.company_name = p.company_name
  AND sc.source_endpoints->>p.source = p.endpoint
  AND NOT EXISTS (
    SELECT 1 FROM source_companies o
    WHERE o.id <> sc.id AND o.source_endpoints->>p.new_source = p.new_endpoint
  )
RETURNING sc.company_name, p.source AS old_source, p.new_source, p.new_endpoint;

-- 3. Disable junk boards and companies whose only board is gone.
UPDATE source_companies sc
SET enabled = FALSE,
    updated_at = CURRENT_TIMESTAMP
FROM plan p
WHERE p.action = 'disable_company' AND p.confidence = 'high'
  AND sc.company_name = p.company_name
  AND sc.source_endpoints->>p.source = p.endpoint
RETURNING sc.company_name, p.reason;

-- Check the RETURNING lists, then:  COMMIT;   (or ROLLBACK;)
-- Medium/low rows are left for manual review.
"""


def build_plan(by_name, health, probe_zero: bool, offline: bool, workers: int) -> list[dict]:
    failing_keys = {(f["n"], f["src"]) for f in health.get("failing", [])}
    zero_keys = {(z["n"], z["src"]) for z in health.get("zero", [])}
    plan, ep_owner = plan_duplicates(by_name)
    dup_keys = {(r["company_name"], r["source"]) for r in plan}

    def company_for(name: str, source: str) -> Optional[Company]:
        for c in by_name.get(name, []):
            if source in c.sources:
                return c
        return None

    jobs = []
    for f in health.get("failing", []):
        c = company_for(f["n"], f["src"])
        if c and (c.name, f["src"]) not in dup_keys:
            jobs.append(("fail", c, f["src"], f.get("reason", "")))
    if probe_zero:
        for z in health.get("zero", []):
            c = company_for(z["n"], z["src"])
            if c and (c.name, z["src"]) not in dup_keys:
                jobs.append(("zero", c, z["src"], ""))

    def run(job):
        kind, c, src, reason = job
        ep = c.sources[src]
        if kind == "fail":
            return plan_failure(c, src, ep, reason, failing_keys, zero_keys, ep_owner, offline)
        return plan_zero(c, src, ep, failing_keys, zero_keys, ep_owner, offline)

    total = len(jobs)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, row in enumerate(pool.map(run, jobs), 1):
            plan.append(row)
            print(f"[{i}/{total}] {row['company_name'][:40]} ({row['source']}): "
                  f"{row['action']} {row['new_source']}:{row['new_endpoint']}".rstrip(": ")
                  + f" [{row['confidence']}]")
    return plan


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("companies", help="companies.json exported from the admin Companies page")
    ap.add_argument("health", help="health.json exported from the admin Scraper health page")
    ap.add_argument("--zero", action="store_true", help="also check boards with 0 jobs for 7 days")
    ap.add_argument("--offline", action="store_true", help="no network: classify and dedupe only")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--output", default="plan.csv")
    args = ap.parse_args(argv)

    by_name = load_companies(args.companies)
    health = json.load(open(args.health, encoding="utf-8"))
    plan = build_plan(by_name, health, args.zero, args.offline, args.workers)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_COLS)
        w.writeheader()
        w.writerows(plan)
    sql_path = os.path.join(os.path.dirname(os.path.abspath(args.output)), "apply_plan.sql")
    with open(sql_path, "w", encoding="utf-8") as f:
        f.write(APPLY_SQL.replace("'plan.csv'", f"'{os.path.basename(args.output)}'"))

    counts: dict[tuple, int] = defaultdict(int)
    for r in plan:
        counts[(r["action"], r["confidence"])] += 1
    print(f"\nWrote {len(plan)} rows to {args.output} and {sql_path}")
    for (action, conf), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {action:16s} {conf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
