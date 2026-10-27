#!/usr/bin/env python3
"""
Find where companies with dead job boards are hiring now.

A Greenhouse (or Lever/Ashby/SmartRecruiters) board that returns 404 usually
means the company moved: a new board token on the same ATS, a different ATS,
or its own careers site. This script looks for the new board and writes a
review CSV. It never writes to the database.

Usage:
    pip install requests
    python scripts/resolve_dead_boards.py dead_boards.csv
    python scripts/resolve_dead_boards.py dead_boards.csv --output relocations.csv --workers 8

Input CSV columns (header required):
    company_name, source, old_endpoint            required
    domain                                        optional (company website)

Full input list (read-only export, run by whoever has DB access):

    \\copy (
      SELECT sc.company_name, r.source_name AS source,
             sc.source_endpoints->>r.source_name AS old_endpoint, sc.domain
      FROM source_companies sc
      JOIN (SELECT DISTINCT company_id, source_name
            FROM scrape_company_results
            WHERE http_status = 404 AND finished_at > NOW() - INTERVAL '7 days') r
        ON r.company_id = sc.id
      WHERE sc.enabled = TRUE
    ) TO 'dead_boards.csv' CSV HEADER

Output CSV columns:
    company_name, old_source, old_endpoint,
    status       relocated | unsupported_ats | junk | not_found | error
    new_source   ashby | greenhouse | lever | smartrecruiters | workday | successfactors
                 (or the unsupported ATS name, e.g. icims)
    new_endpoint value in the format source_endpoints stores for new_source
    jobs         open jobs on the new board when known
    confidence   high | medium | low  (review anything below high)
    evidence     how it was found

How a new board is found, in order:
  1. Slug probes: the old slug, slugs derived from the company name, and the
     company's domain name, tried on Greenhouse, Ashby, Lever and
     SmartRecruiters public APIs.
  2. Careers page: the company's site (input domain, else a verified guess)
     is scanned for links to any ATS, including ones Harco doesn't scrape.
A hit is "high" only with ownership evidence: the board's own company name
matches, the company name appears in its job posts, or the link came from the
company's own careers page.
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import os

import requests

# Run as `python scripts/resolve_dead_boards.py`, Python puts scripts/ on the
# path, not the repo root, so `utils.*` would not import. Add the root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logger = logging.getLogger("resolve_dead_boards")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 12
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

OUT_COLS = [
    "company_name", "old_source", "old_endpoint", "status",
    "new_source", "new_endpoint", "jobs", "confidence", "evidence",
]

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "sa", "ag", "pbc", "lp", "llp",
}
_SLUG_SUFFIXES = (
    "careers", "jobs", "job", "hiring", "hq", "inc", "talent", "team",
    "global", "external", "recruiting", "board",
)
_STOPWORDS = _LEGAL_SUFFIXES | {
    "the", "a", "an", "of", "and", "for", "group", "careers", "jobs", "global",
    "holdings", "international", "technologies", "technology", "solutions",
    "services", "systems", "partners", "labs", "studio", "studios", "us", "usa",
}

# Boards that should be removed, not relocated: tests, sandboxes, internal and
# referral-only boards.
_JUNK_RE = re.compile(
    r"\btest\b|test$|sandbox|referral|internal (?:posts|use)|internal employees|"
    r"\(internal|volunteer$",
    re.IGNORECASE,
)


def is_junk(company_name: str, old_endpoint: str) -> bool:
    return bool(_JUNK_RE.search(company_name or "") or _JUNK_RE.search(old_endpoint or ""))


SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA


# --------------------------------------------------------------------------
# Name helpers
# --------------------------------------------------------------------------
def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def core_name(company_name: str) -> str:
    """Company name without legal suffixes or bracketed notes, lowercase."""
    name = re.sub(r"\(.*?\)", " ", company_name or "")
    toks = [t for t in _tokens(name)]
    while toks and toks[-1] in _LEGAL_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def name_matches(company_name: str, candidate: str) -> bool:
    """True if `candidate` (a board/company display name) is the same company."""
    ta = core_name(company_name).split()
    tb = core_name(candidate).split()
    if not ta or not tb:
        return False
    if "".join(ta) == "".join(tb):          # "Runpod" vs "RunPod", "Hub Spot" vs "HubSpot"
        return True
    # "Visier Solutions" vs "Visier": the shorter name must be the leading
    # whole words of the longer one, and every dropped word must be generic.
    # Character prefixes don't count ("Tempo" is not "Temporal"), real words
    # don't either ("Galileo" is not "Galileo Financial Technologies"), and
    # short names are too ambiguous ("Loop" vs "Loop Industries").
    short, long_ = sorted((ta, tb), key=len)
    if len("".join(short)) < 5 or long_[: len(short)] != short:
        return False
    return all(w in _STOPWORDS for w in long_[len(short):])


def mentioned_in(company_name: str, texts: list[str]) -> bool:
    """True if the company's core name appears as a phrase in any text."""
    phrase = core_name(company_name)
    if len(phrase.replace(" ", "")) < 4:
        return False
    pat = re.compile(r"\b" + r"\s*".join(map(re.escape, phrase.split())) + r"\b", re.I)
    return any(pat.search(t or "") for t in texts)


def slug_candidates(company_name: str, old_endpoint: str, domain: Optional[str]) -> list[str]:
    """Plausible board slugs for this company, most likely first."""
    out: list[str] = []

    def add(s: Optional[str]) -> None:
        s = (s or "").strip()
        if s and len(s) >= 2 and s not in out:
            out.append(s)

    old = (old_endpoint or "").strip().strip("/")
    old = old.rsplit("/", 1)[-1] if "/" in old else old
    add(old)
    add(re.sub(r"\d+$", "", old))                        # verkada2 -> verkada
    for suf in _SLUG_SUFFIXES:                           # hubspotjobs -> hubspot
        if old.lower().endswith(suf) and len(old) > len(suf) + 2:
            add(old[: -len(suf)].rstrip("-_"))

    core = core_name(company_name)
    words = [w for w in core.split() if w]
    if words:
        add("".join(words))                              # visiersolutions
        add("-".join(words))                             # visier-solutions
        distinctive = [w for w in words if w not in _STOPWORDS]
        if distinctive:
            add("".join(distinctive))                    # visier
            add(distinctive[0])
    if domain:
        host = re.sub(r"^(?:https?://)?(?:www\.)?", "", domain.strip().lower()).split("/")[0]
        label = host.split(".")[0] if host else ""
        if label not in {"careers", "jobs", "job", "career"}:
            add(label)                                   # fivetran.com -> fivetran
    return out[:8]


# --------------------------------------------------------------------------
# ATS probes. Each returns a Hit or None. A probe never raises.
# --------------------------------------------------------------------------
@dataclass
class Hit:
    source: str
    endpoint: str
    jobs: Optional[int]
    board_names: list[str] = field(default_factory=list)  # display names from the ATS
    texts: list[str] = field(default_factory=list)        # job text for mention checks
    evidence: str = ""
    confidence: str = "low"


def _get(url: str, **kw) -> Optional[requests.Response]:
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=TIMEOUT, **kw)
        except requests.RequestException:
            r = None
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            return r
        time.sleep(1.5 * (attempt + 1))
    return r


def _json(r: Optional[requests.Response]):
    if r is None or r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def probe_greenhouse(slug: str) -> Optional[Hit]:
    board = _json(_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}"))
    if not isinstance(board, dict):
        return None
    jobs = _json(_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"))
    job_list = jobs.get("jobs", []) if isinstance(jobs, dict) else []
    names = [board.get("name") or ""] + [j.get("company_name") or "" for j in job_list[:5]]
    return Hit("greenhouse", slug, len(job_list), [n for n in names if n])


def probe_ashby(slug: str) -> Optional[Hit]:
    data = _json(_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"))
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    jobs = data.get("jobs") or []
    texts = [(j.get("descriptionPlain") or "")[:4000] for j in jobs[:5]]
    return Hit("ashby", slug, len(jobs), texts=texts)


def probe_lever(slug: str) -> Optional[Hit]:
    for api in ("https://api.lever.co", "https://api.eu.lever.co"):
        data = _json(_get(f"{api}/v0/postings/{slug}", params={"mode": "json"}))
        if isinstance(data, list):
            texts = [
                ((p.get("descriptionPlain") or "") + " " + (p.get("additionalPlain") or ""))[:4000]
                for p in data[:5]
            ]
            return Hit("lever", slug, len(data), texts=texts,
                       evidence="eu" if "eu." in api else "")
    return None


def probe_smartrecruiters(slug: str) -> Optional[Hit]:
    data = _json(_get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        params={"limit": 5},
    ))
    if not isinstance(data, dict) or not data.get("totalFound"):
        # Unknown identifiers return 200 with totalFound=0; nothing to scrape.
        return None
    names = [((p.get("company") or {}).get("name") or "") for p in data.get("content") or []]
    return Hit("smartrecruiters", slug, int(data["totalFound"]), [n for n in names if n])


PROBES: dict[str, Callable[[str], Optional[Hit]]] = {
    "greenhouse": probe_greenhouse,
    "ashby": probe_ashby,
    "lever": probe_lever,
    "smartrecruiters": probe_smartrecruiters,
}


# --------------------------------------------------------------------------
# Careers-page scan
# --------------------------------------------------------------------------
_ATS_LINK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?!embed\b)([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9._-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9._-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
]
_WORKDAY_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
    re.I,
)
_SF_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.jobs2web\.com|career\d*\.successfactors\.(?:com|eu))/[^\s\"'<>]*",
    re.I,
)
_UNSUPPORTED = [
    ("icims", r"\.icims\.com"), ("workable", r"apply\.workable\.com|\.workable\.com/"),
    ("bamboohr", r"\.bamboohr\.com"), ("recruitee", r"\.recruitee\.com"),
    ("jobvite", r"jobs\.jobvite\.com|\.jobvite\.com"), ("taleo", r"\.taleo\.net"),
    ("ukg", r"\.ultipro\.com|\.ukg\.net"), ("paylocity", r"recruiting\.paylocity\.com"),
    ("jazzhr", r"\.applytojob\.com"), ("breezy", r"\.breezy\.hr"),
    ("teamtailor", r"\.teamtailor\.com"), ("personio", r"\.jobs\.personio\.(?:de|com)"),
    ("pinpoint", r"\.pinpointhq\.com"), ("rippling", r"ats\.rippling\.com"),
    ("dover", r"app\.dover\.com/jobs"), ("oracle", r"\.oraclecloud\.com/hcmUI/CandidateExperience"),
    ("eightfold", r"\.eightfold\.ai/careers"), ("adp", r"workforcenow\.adp\.com"),
]
_SKIP_SLUGS = {"embed", "api", "static", "assets", "js", "css", "favicon.ico", "robots.txt"}


def find_ats_links(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ([(source, endpoint)...] supported, [ats_name...] unsupported) found in text."""
    found: list[tuple[str, str]] = []

    def add(src: str, ep: str) -> None:
        if (src, ep) not in found:
            found.append((src, ep))

    for src, pat in _ATS_LINK_PATTERNS:
        for m in pat.finditer(text):
            slug = m.group(1).rstrip(".")
            if slug.lower() not in _SKIP_SLUGS:
                add(src, slug)
    for m in _WORKDAY_RE.finditer(text):
        tenant, shard, site = m.group(1), m.group(2), m.group(3)
        if "impl" not in shard.lower() and site.lower() not in {"wday", "login"}:
            add("workday", f"https://{tenant}.{shard}.myworkdayjobs.com/{site}")
    for m in _SF_RE.finditer(text):
        url = m.group(0)
        parsed = urlparse(url)
        company = (parse_qs(parsed.query).get("company") or [None])[0]
        if parsed.hostname and parsed.hostname.endswith("jobs2web.com"):
            add("successfactors", f"https://{parsed.hostname}")
        elif company:
            add("successfactors", f"https://{parsed.hostname}/career?company={company}")
    unsupported = [name for name, pat in _UNSUPPORTED if re.search(pat, text, re.I)]
    return found, unsupported


def _careers_urls(domain: str) -> list[str]:
    host = re.sub(r"^(?:https?://)?(?:www\.)?", "", domain.strip().lower()).split("/")[0]
    if not host:
        return []
    return [
        f"https://{host}/careers", f"https://www.{host}/careers", f"https://{host}/jobs",
        f"https://careers.{host}", f"https://jobs.{host}", f"https://{host}",
    ]


def scan_careers(domain: str) -> tuple[list[tuple[str, str]], list[str], str]:
    """Fetch the company's careers pages; return (supported, unsupported, page_url)."""
    for url in _careers_urls(domain):
        r = _get(url, allow_redirects=True)
        if r is None or r.status_code >= 400:
            continue
        blob = (r.url or "") + "\n" + (r.text or "")[:2_000_000]
        supported, unsupported = find_ats_links(blob)
        if supported or unsupported:
            return supported, unsupported, r.url or url
    return [], [], ""


def _guess_domain(company_name: str, old_endpoint: str) -> Optional[str]:
    """Verified domain guess, reusing the scraper's resolver when available."""
    try:
        from utils.domain_resolver import resolve_domain  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        return resolve_domain(company_name, old_endpoint)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------
def site_matches_company(company_name: str, url_or_host: Optional[str]) -> bool:
    """True when a website's name is the company's name: 'github.com' for
    GitHub, 'getdbt.com' for dbt. Used to decide whether a guessed website can
    be trusted ('clarity.com' for Hiive cannot)."""
    if not url_or_host:
        return False
    host = urlparse(url_or_host if "://" in url_or_host else "https://" + url_or_host).hostname or ""
    labels = [l for l in host.lower().split(".") if l not in {"www", "careers", "jobs", "job", "career"}]
    if not labels:
        return False
    label = re.sub(r"[^a-z0-9]", "", labels[0])
    core = re.sub(r"[^a-z0-9]", "", core_name(company_name))
    words = [w for w in core_name(company_name).split() if w not in _STOPWORDS]
    first = re.sub(r"[^a-z0-9]", "", words[0]) if words else ""
    if len(core) <= 4:
        return False  # 'mach', 'kit', 'sa': too short to tell one company from another
    names = {core, first} - {""}
    candidates = names | {f"{p}{n}" for n in names for p in ("get", "join", "try", "use")} \
        | {f"{n}{q}" for n in names for q in ("hq", "app", "inc")}
    return (label in candidates
            or core in label                                     # 'bitly' in 'bitlycareers'
            or (len(label) >= 6 and core.startswith(label)))     # 'thinkingmachines' for '... Lab'



def _slug_matches_company(company_name: str, endpoint: str) -> bool:
    """The new board's slug/tenant carries the company's name ('alpine-internships'
    for Alpine Investors, 'carbon' for Carbon, Inc.)."""
    ep = endpoint.lower()
    if "myworkdayjobs.com" in ep:
        ep = (urlparse(endpoint).hostname or "").split(".")[0]
    ep = re.sub(r"[^a-z0-9]", "", ep)
    words = [re.sub(r"[^a-z0-9]", "", w) for w in core_name(company_name).split() if w not in _STOPWORDS]
    return any(len(w) >= 3 and w in ep for w in words)


def _score(hit: Hit, company_name: str, from_careers_page: bool) -> Hit:
    if from_careers_page:
        hit.confidence = "high"
        hit.evidence = (hit.evidence + " linked from company careers page").strip()
    elif any(name_matches(company_name, n) for n in hit.board_names):
        hit.confidence = "high"
        hit.evidence = (hit.evidence + f" board name '{hit.board_names[0]}' matches").strip()
    elif mentioned_in(company_name, hit.texts):
        hit.confidence = "high"
        hit.evidence = (hit.evidence + " company named in job posts").strip()
    elif len(hit.endpoint) >= 6 and not hit.board_names:
        hit.confidence = "medium"
        hit.evidence = (hit.evidence + " slug match only").strip()
    else:
        hit.confidence = "low"
        hit.evidence = (hit.evidence + " slug match, name not confirmed").strip()
    if hit.jobs == 0 and hit.confidence != "low":
        hit.confidence = "medium" if hit.confidence == "high" else "low"
        hit.evidence += "; 0 open jobs"
    return hit


def _better(a: Optional[Hit], b: Optional[Hit]) -> Optional[Hit]:
    if a is None:
        return b
    if b is None:
        return a
    ka = (CONFIDENCE_RANK[a.confidence], a.jobs or 0)
    kb = (CONFIDENCE_RANK[b.confidence], b.jobs or 0)
    return b if kb > ka else a


def _redirected(domain: str, page: str) -> bool:
    """Careers page ended up on a different site than the one requested."""
    a = re.sub(r"^www\.", "", (urlparse(domain if "://" in domain else "https://" + domain).hostname or "").lower())
    b = re.sub(r"^www\.", "", (urlparse(page).hostname or "").lower())
    return bool(a and b) and not (b == a or b.endswith("." + a))


def resolve_one(row: dict, guess_domain: bool = True) -> dict:
    company = (row.get("company_name") or "").strip()
    old_source = (row.get("source") or "").strip().lower()
    old_endpoint = (row.get("old_endpoint") or "").strip()
    domain = (row.get("domain") or "").strip() or None
    domain_from_record = bool(domain)
    trusted = False
    result = {
        "company_name": company, "old_source": old_source, "old_endpoint": old_endpoint,
        "status": "not_found", "new_source": "", "new_endpoint": "", "jobs": "",
        "confidence": "", "evidence": "",
    }
    if is_junk(company, old_endpoint):
        result.update(status="junk", evidence="test/sandbox/internal/referral board; remove, don't relocate")
        return result
    try:
        best: Optional[Hit] = None

        tried: set[str] = set()

        def probe_slugs(slugs: list[str], note: str = "") -> None:
            nonlocal best
            for slug in slugs:
                if slug.lower() in tried:
                    continue
                tried.add(slug.lower())
                for src, probe in PROBES.items():
                    if src == old_source and slug.lower() == old_endpoint.lower():
                        continue  # the dead board itself
                    hit = probe(slug)
                    if hit:
                        hit.evidence = note
                        best = _better(best, _score(hit, company, from_careers_page=False))
                if best and best.confidence == "high":
                    return

        # 1. Slug probes on every supported ATS.
        probe_slugs(slug_candidates(company, old_endpoint, domain))

        # 2. Careers page, when slug probes didn't produce a confirmed board.
        unsupported: list[str] = []
        if not best or best.confidence != "high":
            if not domain and guess_domain:
                domain = _guess_domain(company, old_endpoint)
            if domain:
                supported, unsupported, page = scan_careers(domain)
                # A guessed website is only trusted when its name is the company's
                # (and the careers page didn't redirect to a different company).
                trusted = bool(page) and (domain_from_record and not _redirected(domain, page)
                                          or site_matches_company(company, page))
                for src, ep in supported:
                    if src == old_source and ep.lower() == old_endpoint.lower():
                        continue
                    if src in PROBES:
                        hit = PROBES[src](ep)
                        if hit:
                            hit.evidence = f"{page}:"
                            hit = _score(hit, company, from_careers_page=True)
                            if not trusted and not _slug_matches_company(company, ep):
                                hit.confidence = "medium"
                                hit.evidence += " (website was guessed and doesn't match the company name)"
                            best = _better(best, hit)
                    else:  # workday / successfactors: link found, not probed here
                        conf = "high" if trusted or _slug_matches_company(company, ep) else "medium"
                        best = _better(best, Hit(src, ep, None, evidence=f"{page}: linked from company careers page", confidence=conf))
                # Careers page redirected to another company's domain (an
                # acquisition, e.g. dbt Labs -> fivetran.com): try that name.
                if (not best or best.confidence != "high") and page:
                    host = (urlparse(page).hostname or "").removeprefix("www.")
                    label = host.split(".")[0] if host else ""
                    if label and label not in {"careers", "jobs", "job", "career"}:
                        probe_slugs([label], note=f"careers page redirects to {host};")

        if best:
            result.update(
                status="relocated", new_source=best.source, new_endpoint=best.endpoint,
                jobs="" if best.jobs is None else str(best.jobs),
                confidence=best.confidence, evidence=best.evidence,
            )
        elif unsupported:
            result.update(
                status="unsupported_ats", new_source=unsupported[0],
                confidence="high" if trusted else "medium",
                evidence=f"careers page uses {', '.join(unsupported)}"
                         + ("" if trusted else " (website was guessed and doesn't match the company name)"),
            )
        else:
            result["evidence"] = "no live board found" + ("" if domain else "; no domain known")
    except Exception as exc:  # noqa: BLE001
        result.update(status="error", evidence=f"{type(exc).__name__}: {exc}"[:300])
    return result


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="CSV with company_name, source, old_endpoint[, domain]")
    ap.add_argument("--output", default="relocations.csv")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-guess-domain", action="store_true",
                    help="only scan careers pages for rows that have a domain")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    with open(args.input, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("company_name") or "").strip()]
    if not rows:
        print("No rows in input.", file=sys.stderr)
        return 1

    print(f"Resolving {len(rows)} companies with {args.workers} workers...")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(resolve_one, r, not args.no_guess_domain) for r in rows]
        for i, fut in enumerate(futures, 1):
            res = fut.result()
            results.append(res)
            print(f"[{i}/{len(rows)}] {res['company_name']}: {res['status']} "
                  f"{res['new_source']} {res['new_endpoint']} {res['confidence']}".rstrip())

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(results)

    counts: dict[str, int] = {}
    for r in results:
        key = r["status"] + (f" ({r['confidence']})" if r["status"] == "relocated" else "")
        counts[key] = counts.get(key, 0) + 1
    print(f"\nWrote {len(results)} rows to {args.output}")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {v:5d}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
