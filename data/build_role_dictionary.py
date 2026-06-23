"""
Build data/role_title_lookup.json from O*NET Alternate Titles (db 29.0).

Downloads the public-domain tab-delimited file from onetcenter.org, applies a
SOC-group → role_function mapping (documented below), normalises every alternate
title, and writes a compact JSON lookup used by utils/role_classifier.py.

Usage:
    python data/build_role_dictionary.py
    python data/build_role_dictionary.py --onet-path data/onet_raw/Alternate_Titles.txt

Raw download is saved to data/onet_raw/ (gitignored).
Output: data/role_title_lookup.json  (committed)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None  # type: ignore

ONET_URL = (
    "https://www.onetcenter.org/dl_files/database/db_29_0_text/Alternate%20Titles.txt"
)
RAW_DIR = Path(__file__).parent / "onet_raw"
RAW_FILE = RAW_DIR / "Alternate_Titles.txt"
OUT_FILE = Path(__file__).parent / "role_title_lookup.json"

# ---------------------------------------------------------------------------
# SOC → role_function mapping
#
# Three levels of specificity (looked up in order, most-specific first):
#   1. Unit group:  "XX-YYYY"   (6-char code before the O*NET decimal)
#   2. Minor group: "XX-Y"      (4-char prefix — first minor digit only)
#   3. Major group: "XX"        (2-char prefix)
#
# SOC major groups (BLS 2018 SOC):
#   11 Management | 13 Business & Finance | 15 Computer & Math
#   17 Architecture & Engineering | 19 Life/Physical/Social Science
#   21 Community & Social Service | 23 Legal | 25 Education
#   27 Arts, Design & Media | 29 Healthcare Practitioners
#   31 Healthcare Support | 33 Protective Service | 35 Food Preparation
#   37 Building & Grounds | 39 Personal Care | 41 Sales
#   43 Office & Admin Support | 45 Farming | 47 Construction
#   49 Installation & Repair | 51 Production | 53 Transportation
# ---------------------------------------------------------------------------

# Unit-group overrides (7-char "XX-YYYY"): highest priority
_SOC_UNIT: dict[str, str] = {
    # ---- 11: Management ----
    "11-3021": "software_engineering",       # Computer & Info Systems Managers
    "11-3031": "finance_accounting",         # Financial Managers
    "11-3051": "manufacturing",              # Industrial Production Managers
    "11-3071": "project_program_management", # Transportation/Storage Managers
    "11-9041": "hardware_engineering",       # Architectural & Engineering Managers
    "11-9121": "product_management",         # Natural Sciences Managers
    # ---- 13: Business & Finance ----
    "13-1061": "hr_recruiting",              # Human Resources Specialists
    "13-1071": "consulting",                 # Management Analysts
    "13-1082": "project_program_management", # Project Management Specialists
    "13-1111": "consulting",                 # Management Analysts (alt code)
    "13-1141": "hr_recruiting",              # Compensation & Benefits Specialists
    "13-1151": "hr_recruiting",              # Training & Development Specialists
    "13-1161": "marketing",                  # Market Research Analysts
    "13-1199": "business_operations",        # Business Operations, All Other
    # ---- 15: Computer & Math ----
    "15-1111": "data_ml",                    # Computer & Info Research Scientists
    "15-1121": "software_engineering",       # Computer Systems Analysts
    "15-1122": "security",                   # Information Security Analysts
    "15-1131": "software_engineering",       # Computer Programmers
    "15-1132": "software_engineering",       # Software Developers, Applications
    "15-1133": "qa",                         # Software QA Analysts & Testers
    "15-1134": "software_engineering",       # Web Developers
    "15-1141": "devops_sre",                 # Database Administrators
    "15-1142": "devops_sre",                 # Network & Computer Systems Admins
    "15-1143": "devops_sre",                 # Computer Network Architects
    "15-1151": "it_support",                 # Computer User Support Specialists
    "15-1152": "it_support",                 # Computer Network Support Specialists
    "15-1199": "software_engineering",       # Computer Occupations, All Other
    # SOC 2018 revised codes for 15-xxxx
    "15-1211": "data_ml",                    # Computer & Information Research Scientists
    "15-1212": "security",                   # Information Security Analysts (2018)
    "15-1221": "software_engineering",       # Computer & Information Analysts (2018)
    "15-1231": "devops_sre",                 # Computer Network Support Specialists (2018)
    "15-1241": "devops_sre",                 # Computer Network Architects (2018)
    "15-1244": "devops_sre",                 # Network & Systems Admins (2018)
    "15-1251": "software_engineering",       # Computer Programmers (2018)
    "15-1252": "software_engineering",       # Software Developers (2018)
    "15-1253": "qa",                         # Software QA Analysts (2018)
    "15-1254": "design_ux",                  # Web & Digital Interface Designers
    "15-1255": "software_engineering",       # Web Developers (2018)
    "15-1299": "it_support",                 # Computer Occupations, All Other (2018)
    # ---- 17: Architecture & Engineering ----
    "17-2061": "hardware_engineering",       # Computer Hardware Engineers
    # ---- 19: Science ----
    "19-3021": "data_ml",                    # Actuaries (math-heavy)
    "19-4041": "lab_technician",             # Geological & Petroleum Technicians
    "19-4051": "lab_technician",             # Nuclear Technicians
    "19-4061": "lab_technician",             # Social Science Research Assistants
    # ---- 27: Arts & Design ----
    "27-1014": "design_ux",                  # Multimedia Artists & Animators
    "27-1021": "design_ux",                  # Commercial & Industrial Designers
    "27-1024": "design_ux",                  # Graphic Designers
    "27-1025": "design_ux",                  # Interior Designers
    "27-3011": "content_writing",            # Broadcast Announcers & Radio DJs
    "27-3031": "content_writing",            # Public Relations Specialists
    "27-3041": "content_writing",            # Editors
    "27-3042": "content_writing",            # Technical Writers
    "27-3043": "content_writing",            # Writers & Authors
    # ---- 29: Healthcare ----
    "29-1141": "nursing",                    # Registered Nurses
    "29-1151": "nursing",                    # Nurse Anesthetists
    "29-1161": "nursing",                    # Nurse Midwives
    "29-1171": "nursing",                    # Nurse Practitioners
    # ---- 41: Sales ----
    "41-2000": "retail",                     # Retail Sales Workers (group)
    "41-2011": "retail",                     # Cashiers
    "41-2021": "retail",                     # Counter & Rental Clerks
    "41-2031": "retail",                     # Retail Salespersons
    "41-3000": "sales",                      # Sales Representatives (services)
    "41-4000": "sales",                      # Sales Representatives (wholesale/mfg)
    # ---- 43: Admin ----
    "43-3031": "finance_accounting",         # Bookkeeping, Accounting, Auditing Clerks
    "43-3051": "finance_accounting",         # Payroll & Timekeeping Clerks
    "43-6014": "business_operations",        # Secretaries & Admin Assistants (non-legal/med)
}

# Minor-group overrides ("XX-Y" — first 4 chars of "XX-YYYY"):
_SOC_MINOR: dict[str, str] = {
    # ---- 11: Management ----
    "11-1": "business_operations",    # Top Executives
    "11-2": "marketing",              # Advertising, Marketing & Promotions Managers
    "11-3": "business_operations",    # Operations Specialties Managers (default)
    "11-9": "project_program_management",  # Other Managers (default)
    # ---- 13: Business & Finance ----
    "13-1": "business_operations",    # Business Operations Specialists (default)
    "13-2": "finance_accounting",     # Financial Specialists
    # ---- 15: Computer & Math ----
    "15-1": "software_engineering",   # Computer Occupations (default)
    "15-2": "data_ml",                # Mathematical Science Occupations
    # ---- 17: Architecture & Engineering ----
    "17-1": "hardware_engineering",   # Architects, except Naval
    "17-2": "hardware_engineering",   # Engineers (non-software default)
    "17-3": "hardware_engineering",   # Drafters, Engineering & Mapping Technicians
    # ---- 19: Science ----
    "19-1": "research_science",       # Life Scientists
    "19-2": "research_science",       # Physical Scientists
    "19-3": "other",                  # Social Scientists (psychologists, economists, etc.)
    "19-4": "lab_technician",         # Life, Physical & Social Science Technicians
    # ---- 21: Community & Social Service ----
    "21-1": "other",                  # Counselors, Social Workers
    "21-2": "other",                  # Religious Workers
    # ---- 23: Legal ----
    "23-1": "legal",                  # Lawyers & Judges
    "23-2": "legal",                  # Legal Support Workers
    # ---- 25: Education ----
    "25-1": "teaching",               # Postsecondary Teachers
    "25-2": "teaching",               # K–12 Teachers
    "25-3": "teaching",               # Other Teachers & Instructors
    "25-4": "education_admin",        # Librarians, Curators & Archivists
    "25-9": "education_admin",        # Other Education, Training & Library
    # ---- 27: Arts, Design, Media ----
    "27-1": "design_ux",              # Art & Design Workers (default)
    "27-2": "media_production",       # Entertainers & Performers
    "27-3": "content_writing",        # Media & Communication Workers (default)
    "27-4": "media_production",       # Media & Communication Equipment Workers
    # ---- 29: Healthcare Practitioners ----
    "29-1": "clinical",               # Health Diagnosing & Treating Practitioners
    "29-2": "allied_health",          # Health Technologists & Technicians
    "29-9": "healthcare_admin",       # Other Healthcare Practitioners
    # ---- 31: Healthcare Support ----
    "31-1": "allied_health",          # Home Health & Personal Care Aides
    "31-2": "allied_health",          # Occupational/Physical Therapist Assistants
    "31-9": "allied_health",          # Other Healthcare Support
    # ---- 33: Protective Service ----
    "33-1": "other",                  # First-line Supervisors, Protective Service
    "33-2": "other",                  # Firefighting & Prevention Workers
    "33-3": "other",                  # Law Enforcement Workers
    "33-9": "other",                  # Other Protective Service Workers
    # ---- 35: Food Preparation ----
    "35-1": "hospitality_food",       # Supervisors, Food Preparation & Service
    "35-2": "hospitality_food",       # Cooks & Food Preparation Workers
    "35-3": "hospitality_food",       # Food & Beverage Serving Workers
    "35-9": "hospitality_food",       # Other Food Preparation & Serving Related
    # ---- 37: Building & Grounds ----
    "37-1": "general_labor",          # Building Cleaning & Pest Control Supervisors
    "37-2": "general_labor",          # Building Cleaning Workers
    "37-3": "general_labor",          # Grounds Maintenance Workers
    # ---- 39: Personal Care ----
    "39-1": "customer_service",       # Supervisors, Personal Care & Service
    "39-3": "customer_service",       # Entertainment Attendants & Related
    "39-5": "customer_service",       # Personal Appearance Workers
    "39-6": "customer_service",       # Baggage Porters, Bellhops & Concierges
    "39-7": "customer_service",       # Tour & Travel Guides
    "39-9": "customer_service",       # Other Personal Care & Service Workers
    # ---- 41: Sales ----
    "41-1": "sales",                  # Supervisors of Sales Workers
    "41-2": "retail",                 # Retail Sales Workers
    "41-3": "sales",                  # Sales Representatives, Services
    "41-4": "sales",                  # Sales Representatives, Wholesale & Mfg
    "41-9": "sales",                  # Other Sales & Related Workers
    # ---- 43: Office & Admin Support ----
    "43-1": "business_operations",    # First-line Supervisors, Admin Support
    "43-2": "business_operations",    # Communications Equipment Operators
    "43-3": "finance_accounting",     # Financial Clerks
    "43-4": "business_operations",    # Information & Record Clerks
    "43-5": "business_operations",    # Material Recording, Scheduling & Distributing
    "43-6": "business_operations",    # Secretaries & Admin Assistants
    "43-9": "business_operations",    # Other Office & Admin Support
    # ---- 45: Farming ----
    "45-1": "general_labor",          # Supervisors, Farming, Fishing & Forestry
    "45-2": "general_labor",          # Agricultural Workers
    "45-3": "general_labor",          # Fishers & Fishing Workers
    "45-4": "general_labor",          # Forest & Conservation Workers
    # ---- 47: Construction & Extraction ----
    "47-1": "skilled_trades",         # Supervisors, Construction & Extraction
    "47-2": "skilled_trades",         # Construction Trades Workers
    "47-3": "skilled_trades",         # Helpers, Construction Trades
    "47-4": "skilled_trades",         # Other Construction & Related Workers
    "47-5": "skilled_trades",         # Extraction Workers
    # ---- 49: Installation, Maintenance & Repair ----
    "49-1": "skilled_trades",         # Supervisors, Installation & Repair
    "49-2": "skilled_trades",         # Electrical & Electronic Equipment Mechanics
    "49-3": "skilled_trades",         # Vehicle & Mobile Equipment Mechanics
    "49-6": "skilled_trades",         # Precision Instrument & Equipment Repairers
    "49-9": "skilled_trades",         # Other Installation, Maintenance & Repair
    # ---- 51: Production ----
    "51-1": "manufacturing",          # Supervisors, Production & Operating Workers
    "51-2": "manufacturing",          # Assemblers & Fabricators
    "51-3": "manufacturing",          # Food Processing Workers
    "51-4": "manufacturing",          # Metal & Plastic Workers
    "51-5": "manufacturing",          # Printing Workers
    "51-6": "manufacturing",          # Textile, Apparel & Furnishings Workers
    "51-7": "manufacturing",          # Woodworkers
    "51-8": "manufacturing",          # Plant & System Operators
    "51-9": "manufacturing",          # Other Production Occupations
    # ---- 53: Transportation & Material Moving ----
    "53-1": "general_labor",          # Supervisors, Transportation & Material Moving
    "53-2": "general_labor",          # Air Transportation Workers
    "53-3": "general_labor",          # Motor Vehicle Operators
    "53-4": "general_labor",          # Rail Transportation Workers
    "53-5": "general_labor",          # Water Transportation Workers
    "53-6": "general_labor",          # Other Transportation Workers
    "53-7": "general_labor",          # Material Moving Workers
}

# Major-group fallback ("XX" — first 2 chars):
_SOC_MAJOR: dict[str, str] = {
    "11": "business_operations",
    "13": "finance_accounting",
    "15": "software_engineering",
    "17": "hardware_engineering",
    "19": "research_science",
    "21": "other",
    "23": "legal",
    "25": "teaching",
    "27": "design_ux",
    "29": "clinical",
    "31": "allied_health",
    "33": "other",
    "35": "hospitality_food",
    "37": "general_labor",
    "39": "customer_service",
    "41": "sales",
    "43": "business_operations",
    "45": "general_labor",
    "47": "skilled_trades",
    "49": "skilled_trades",
    "51": "manufacturing",
    "53": "general_labor",
}

VALID_FUNCTIONS = {
    "software_engineering", "data_ml", "data_analytics", "devops_sre",
    "security", "qa", "it_support", "hardware_engineering",
    "product_management", "project_program_management", "sales", "marketing",
    "customer_success", "finance_accounting", "hr_recruiting", "legal",
    "business_operations", "consulting",
    "clinical", "nursing", "allied_health", "healthcare_admin",
    "design_ux", "content_writing", "media_production",
    "research_science", "lab_technician",
    "teaching", "education_admin",
    "skilled_trades", "manufacturing",
    "hospitality_food", "retail", "customer_service", "general_labor",
    "other",
}


def soc_to_role(soc_code: str) -> str:
    """Map an O*NET SOC code (e.g. '15-1252.00') to a role_function."""
    # Strip O*NET subdivision: "15-1252.00" → "15-1252"
    base = soc_code.split(".")[0].strip()
    # Try unit group (7 chars: "XX-YYYY")
    if base in _SOC_UNIT:
        return _SOC_UNIT[base]
    # Try minor group (4 chars: "XX-Y" — first 4 chars covers "XX-Y")
    minor4 = base[:4]
    if minor4 in _SOC_MINOR:
        return _SOC_MINOR[minor4]
    # Try major group (2 chars before dash: "XX")
    major = base[:2]
    return _SOC_MAJOR.get(major, "other")


# ---------------------------------------------------------------------------
# Title normalisation (mirrors the logic in role_classifier.py — keep identical)
# ---------------------------------------------------------------------------
_SENIORITY_RE = re.compile(
    r"\b(senior|sr\.?|junior|jr\.?|lead|principal|"
    r"associate|head\s+of|entry[\s\-]?level|mid[\s\-]?level|"
    r"experienced|distinguished|fellow|founding|interim|acting)\b"
    r"|\bstaff(?=\s)",
    re.IGNORECASE,
)
_LEVEL_SUFFIX_RE = re.compile(r"\b(i{1,3}v?|[1-4])\s*$", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
# Matches parenthetical expansions e.g. "(Three Dimensional Animator)"
_PAREN = re.compile(r"\([^)]*\)")


def normalize(title: str) -> str:
    t = title.lower().strip()
    t = _SENIORITY_RE.sub("", t)
    t = _LEVEL_SUFFIX_RE.sub("", t)
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def download_onet(dest: Path) -> None:
    if requests is None:
        sys.exit("requests library not available; install it or pass --onet-path")
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading O*NET Alternate Titles -> {dest} ...", flush=True)
    with requests.get(ONET_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    print(f"  Downloaded {dest.stat().st_size:,} bytes.", flush=True)  # noqa: RUF001


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(onet_path: Path, out_path: Path) -> None:
    lookup: dict[str, str] = {}
    conflict: dict[str, set[str]] = {}

    print(f"Parsing {onet_path} ...", flush=True)
    with open(onet_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            soc = row.get("O*NET-SOC Code", "").strip()
            alt_title = row.get("Alternate Title", "").strip()
            if not soc or not alt_title:
                continue
            role = soc_to_role(soc)
            # Split parenthetical expansions before normalising so that
            # "3D Animator (Three Dimensional Animator)" emits two clean keys:
            #   "3d animator"  and  "three dimensional animator"
            outside = _PAREN.sub(" ", alt_title)
            parts = [outside] + [p.strip("()") for p in _PAREN.findall(alt_title)]
            for part in parts:
                key = normalize(part)
                if not key:
                    continue
                if key in lookup:
                    if lookup[key] != role:
                        conflict.setdefault(key, {lookup[key]}).add(role)
                else:
                    lookup[key] = role

    # Resolve conflicts: prefer the more-specific function (non-"other")
    for key, roles in conflict.items():
        roles.add(lookup[key])
        non_other = roles - {"other"}
        if len(non_other) == 1:
            lookup[key] = next(iter(non_other))
        # Multiple non-other roles: keep existing (first-seen wins)

    # Validate
    for v in lookup.values():
        assert v in VALID_FUNCTIONS, f"Unknown role_function: {v!r}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(lookup, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")

    print(
        f"Wrote {len(lookup):,} entries -> {out_path}\n"
        f"  Conflicts resolved: {len(conflict)}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--onet-path",
        type=Path,
        default=None,
        help="Path to already-downloaded Alternate Titles.txt (skips download)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_FILE,
        help=f"Output JSON path (default: {OUT_FILE})",
    )
    args = parser.parse_args()

    onet_path: Path = args.onet_path or RAW_FILE
    if not onet_path.exists():
        download_onet(onet_path)

    build(onet_path, args.out)


if __name__ == "__main__":
    main()
