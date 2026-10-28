"""
Find where a company with a dead job board is hiring now.

Library used by services/source_reconcile.py: slug probes on the public
Greenhouse, Ashby, Lever and SmartRecruiters APIs, careers-page scanning for
any ATS link, and the confidence rules that decide whether a match can be
trusted. Never writes to the database.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse


import requests

logger = logging.getLogger(__name__)

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
    r = SESSION.post(
        url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
        timeout=TIMEOUT, headers={"Accept": "application/json"},
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
        r = SESSION.get(f"https://{tenant}.{shard}.myworkdayjobs.com/",
                            timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    parts = _workday_parts(r.url or "")
    return parts[2] if parts and parts[2] else None


def probe_workday(endpoint: str) -> Optional[Hit]:
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
            return Hit("workday", f"https://{tenant}.{sh}.myworkdayjobs.com/{site}",
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
                return Hit("workday", f"https://{tenant}.{sh}.myworkdayjobs.com/{cand}",
                               total, evidence=f"site changed from '{site}' to '{cand}' ({how})",
                               confidence="high" if cand == default else "medium")
    return None


def same_board(source: str, endpoint: str, hit: Hit) -> bool:
    """True when a 'new' board is the one we already have (same data center too)."""
    if hit.source != source:
        return False
    if source == "workday":
        a, b = _workday_parts(endpoint), _workday_parts(hit.endpoint)
        return bool(a and b) and (a[0], a[1], a[2].lower()) == (b[0], b[1], b[2].lower())
    return endpoint.strip().lower() == hit.endpoint.strip().lower()


def endpoint_key(source: str, endpoint: str) -> str:
    """Identity of a board regardless of URL formatting or Workday data center."""
    ep = endpoint.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
    if source == "workday":
        parts = _workday_parts(endpoint)
        if parts:
            ep = f"{parts[0]}/{parts[2].lower()}"
    return f"{source}:{ep}"
