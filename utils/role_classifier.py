"""
Fine-grained role function classifier.

Sits alongside utils/occupation_category (which provides the coarse 9-category
label); this module adds a single finer field:

    role_function: software_engineering | data_ml | data_analytics | devops_sre |
                   security | qa | it_support | hardware_engineering |
                   product_management | project_program_management | sales |
                   marketing | customer_success | finance_accounting | hr_recruiting |
                   legal | business_operations | consulting |
                   clinical | nursing | allied_health | healthcare_admin |
                   design_ux | content_writing | media_production |
                   research_science | lab_technician |
                   teaching | education_admin |
                   skilled_trades | manufacturing |
                   hospitality_food | retail | customer_service | general_labor |
                   other

classify_role(title, noc_code) -> {role_function, method, confidence}

Cheap-first cascade (stops at first confident hit):
  1. NOC code  → deterministic NOC map             (method="noc")
  2. Title     → dict lookup (O*NET + hand-overrides) (method="dict")
  3. Title     → LLM (only when ROLE_LLM_API_KEY is set) (method="llm")
  No confident hit → role_function="other"           (method="fallback")
"""
from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Taxonomy validation set
# ---------------------------------------------------------------------------
VALID_FUNCTIONS: frozenset[str] = frozenset({
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
})

# ---------------------------------------------------------------------------
# Tier 1: NOC → role_function
#
# Canada's NOC 2021 uses 5-digit codes where:
#   digit 0 = TEER level (0-5)
#   digit 1 = broad occupational category (0-9)
#   digits 2-4 = major/sub-major/unit groups
#
# The older NOC 2016 / NOC 2011 uses 4-digit codes where digit 0 is the broad
# category directly (matches the existing occupation_category.from_noc logic).
#
# Lookup order: exact 5-digit → first 3 digits → first 2 digits → first digit.
# ---------------------------------------------------------------------------

# Exact 5-digit NOC 2021 codes (highest specificity)
_NOC5: dict[str, str] = {
    # --- Computing / IT professionals (21xxx series) ---
    "21100": "data_ml",              # Computer and info research scientists
    "21200": "devops_sre",           # Database analysts and data admins
    "21210": "data_ml",              # Data scientists and data analysts
    "21211": "data_ml",              # Data scientists
    "21220": "security",             # Cybersecurity professionals
    "21221": "software_engineering", # Information systems specialists
    "21222": "software_engineering", # Business system specialists
    "21223": "devops_sre",           # Database analysts (alt code)
    "21230": "hardware_engineering", # Computer engineers
    "21231": "software_engineering", # Software engineers and designers
    "21232": "software_engineering", # Software developers and programmers
    "21233": "design_ux",            # Web designers and UI designers
    "21234": "software_engineering", # App developers and programmers
    "21300": "devops_sre",           # Computer network technicians (TEER 2)
    "21301": "devops_sre",           # Computer network and web technicians
    "21310": "devops_sre",           # Computer network technicians (alt)
    "22220": "devops_sre",           # Computer network technicians (TEER 2 alt)
    "22221": "it_support",           # User support technicians
    "22222": "qa",                   # Information systems testing technicians
    "22223": "devops_sre",           # Computer network support technicians
    # --- Business / management (10xxx, 11xxx, 12xxx) ---
    "10010": "hardware_engineering", # Engineering managers
    "10011": "software_engineering", # Computer and information systems managers
    "10019": "marketing",            # Business dev officers & market researchers
    "10020": "finance_accounting",   # Financial managers
    "10021": "hr_recruiting",        # Human resources managers
    "10022": "business_operations",  # Purchasing managers
    "10023": "business_operations",  # Administrative services managers
    "10029": "project_program_management",  # Other admin services managers
    "11100": "marketing",            # Sales, marketing and advertising managers
    "11101": "sales",                # Sales managers (specific)
    "11102": "marketing",            # Marketing managers
    "11200": "finance_accounting",   # Financial managers (detailed)
    "11201": "hr_recruiting",        # HR managers
    "11202": "hr_recruiting",        # Industrial relations managers
    "12010": "finance_accounting",   # Financial auditors and accountants
    "12011": "finance_accounting",   # Financial and investment analysts
    "12012": "finance_accounting",   # Financial advisors
    "12013": "finance_accounting",   # Securities agents
    "12100": "hr_recruiting",        # HR professionals
    "12101": "hr_recruiting",        # Compensation, benefits and job classification
    "12102": "hr_recruiting",        # Training officers and managers
    "12103": "hr_recruiting",        # Employment counsellors
    "12200": "project_program_management",  # Government program officers
    "13100": "business_operations",  # Admin officers
    "13110": "business_operations",  # Records management
    "13111": "business_operations",  # Management consultants
    "13200": "finance_accounting",   # Insurance adjusters
    "13201": "finance_accounting",   # Customs, ship and other brokers
    # --- Healthcare (30xxx, 31xxx, 32xxx) ---
    "30010": "clinical",             # Specialists in clinical medicine
    "30011": "clinical",             # General practitioners and family physicians
    "30012": "clinical",             # Dentists
    "30020": "clinical",             # Veterinarians
    "31100": "clinical",             # Pharmacists
    "31101": "allied_health",        # Dietitians and nutritionists
    "31102": "clinical",             # Audiologists and speech-language pathologists
    "31103": "allied_health",        # Physiotherapists
    "31104": "allied_health",        # Occupational therapists
    "31200": "nursing",              # Registered nurses
    "31201": "nursing",              # Nurse practitioners (Canada)
    "31209": "nursing",              # Other registered nursing professionals
    "32100": "allied_health",        # Licensed practical nurses
    "32101": "allied_health",        # Psychiatric nurses
    "32120": "allied_health",        # Medical radiation technologists
    "32121": "allied_health",        # Medical sonographers
    "32122": "allied_health",        # Cardiology technologists
    "32123": "allied_health",        # EEG technologists
    "32124": "allied_health",        # Other medical technologists
    "33100": "allied_health",        # Dental assistants
    "33101": "allied_health",        # Dental hygienists
    "33102": "allied_health",        # Pharmacy technicians
    "33103": "allied_health",        # Respiratory therapists
    # --- Education (40xxx) ---
    "40020": "teaching",             # University professors
    "40021": "teaching",             # Post-secondary instructors
    "40030": "teaching",             # Elementary school teachers
    "40031": "teaching",             # Secondary school teachers
    "41200": "legal",                # Lawyers and Quebec notaries
    "41201": "legal",                # Judges
    "41210": "legal",                # Paralegal and related occupations
    "41220": "other",                # Social workers
    "41300": "other",                # Policy researchers
    # --- Creative / media (51xxx, 52xxx) ---
    "51100": "design_ux",            # Architects
    "51110": "design_ux",            # Landscape architects
    "51120": "design_ux",            # Urban and land use planners
    "52100": "design_ux",            # Graphic designers and illustrators
    "52110": "design_ux",            # Interior designers
    "52120": "content_writing",      # Technical writers
    "52121": "content_writing",      # Authors and writers
    "52122": "content_writing",      # Editors
    "52130": "media_production",     # Translators
    # --- Trades (72xxx, 73xxx) ---
    "72010": "skilled_trades",       # Electricians
    "72011": "skilled_trades",       # Industrial electricians
    "72020": "skilled_trades",       # Plumbers
    "72021": "skilled_trades",       # Pipefitters
    "72100": "skilled_trades",       # Carpenters
    "72200": "skilled_trades",       # Welders
    # --- Manufacturing (92xxx) ---
    "92010": "manufacturing",        # Machine operators
    "92100": "manufacturing",        # Textile operators
}

# 3-digit NOC prefix → role_function (both old 4-digit and new 5-digit)
_NOC3: dict[str, str] = {
    # 5-digit NOC 2021 prefixes
    "000": "business_operations",    # Senior managers
    "001": "business_operations",    # Senior managers (other)
    "100": "business_operations",    # Corporate managers — engineering, utilities
    "101": "sales",                  # Retail and wholesale trade managers
    "102": "healthcare_admin",       # Managers in health care
    "103": "education_admin",        # School principals and admin
    "104": "other",                  # Community service managers
    "110": "marketing",              # Advertising, marketing managers
    "111": "hr_recruiting",          # Other business services managers
    "112": "finance_accounting",     # Insurance, real estate managers
    "120": "finance_accounting",     # Financial, insurance professionals
    "121": "hr_recruiting",          # Human resources professionals
    "122": "business_operations",    # Business development professionals
    "123": "business_operations",    # Admin occupations
    "124": "business_operations",    # Office and coordination
    "125": "business_operations",    # Mail and message processing
    "130": "business_operations",    # Admin occupations (TEER 3)
    "131": "finance_accounting",     # Finance/insurance (TEER 3)
    "132": "business_operations",    # Administrative (TEER 3)
    "140": "business_operations",    # Document processing (TEER 4)
    "141": "finance_accounting",     # Finance clerks (TEER 4)
    "142": "business_operations",    # Admin support (TEER 4)
    "143": "business_operations",    # Data entry (TEER 4/5)
    "200": "research_science",       # Scientists (natural and applied, TEER 0 broad 0)
    "201": "research_science",       # Life sciences professionals
    "202": "research_science",       # Physical/earth science professionals
    "203": "research_science",       # Social science professionals
    "210": "data_ml",                # Math and statistics professionals
    "211": "data_ml",                # Computer and information scientists
    "212": "software_engineering",   # Computing, telecom, other IT professionals
    "213": "hardware_engineering",   # Architecture and engineering professionals
    "214": "research_science",       # Life sciences professionals (TEER 1)
    "215": "research_science",       # Physical sciences professionals (TEER 1)
    "216": "other",                  # Social sciences professionals
    "220": "hardware_engineering",   # Engineering technicians
    "221": "hardware_engineering",   # Electrotechnology technicians
    "222": "it_support",             # IT and telecommunications technicians
    "223": "hardware_engineering",   # Architecture/drafting technicians
    "224": "lab_technician",         # Life sciences technicians
    "225": "lab_technician",         # Physical sciences technicians
    "300": "clinical",               # Health professionals (TEER 0)
    "301": "clinical",               # Health treatment professionals
    "302": "nursing",                # Nursing professionals
    "310": "clinical",               # Health professionals (TEER 1)
    "311": "nursing",                # Registered nurses (TEER 1)
    "312": "allied_health",          # Pharmacy and dietetics professionals
    "313": "allied_health",          # Therapy and assessment professionals
    "320": "allied_health",          # Technical health occupations
    "321": "allied_health",          # Medical technicians
    "322": "allied_health",          # Nursing and patient care (TEER 2)
    "323": "allied_health",          # Dental/pharmacy technicians
    "330": "allied_health",          # Assisting health (TEER 3)
    "331": "allied_health",          # Nurse assistants
    "332": "allied_health",          # Dental/pharmacy assistants
    "400": "teaching",               # Judges, lawyers (TEER 0 broad 4)
    "401": "teaching",               # Professors and university (TEER 0/1)
    "402": "teaching",               # K-12 principals
    "410": "legal",                  # Legal professionals
    "411": "teaching",               # University professors (TEER 1)
    "412": "teaching",               # Post-secondary and other instructors
    "413": "teaching",               # Elementary/secondary teachers
    "414": "education_admin",        # Educational counsellors
    "420": "other",                  # Paraprofessional (legal, social)
    "421": "other",                  # Social service workers
    "422": "other",                  # Community service workers
    "423": "other",                  # Correctional officers
    "424": "other",                  # Firefighters, police
    "430": "other",                  # Home childcare
    "431": "other",                  # Elementary school aide
    "440": "other",                  # Service workers (TEER 4 broad 4)
    "500": "design_ux",              # Professional creative/arts (TEER 0)
    "501": "design_ux",              # Architecture professionals
    "510": "design_ux",              # Creative arts professionals (TEER 1)
    "511": "content_writing",        # Authors, writers, journalists
    "512": "design_ux",              # Graphic designers
    "513": "media_production",       # Performing artists
    "514": "media_production",       # Film, TV producers/directors
    "520": "media_production",       # Technical arts occupations (TEER 2)
    "521": "media_production",       # Broadcast/AV technicians
    "522": "design_ux",              # Photography technicians
    "523": "media_production",       # Performing arts technicians
    "530": "retail",                 # Retail/wholesale supervisors
    "531": "customer_service",       # Technical service supervisors
    "540": "customer_service",       # Service supervisors (TEER 3/4)
    "541": "customer_service",       # Security guards
    "542": "customer_service",       # Tour and travel agents
    "550": "hospitality_food",       # Food service workers
    "551": "customer_service",       # Service workers
    "600": "retail",                 # Retail sales workers
    "601": "sales",                  # Sales reps (TEER 0/1)
    "610": "retail",                 # Retail sales workers (TEER 3/4)
    "620": "hospitality_food",       # Food/travel/accommodation (TEER 3)
    "621": "customer_service",       # Hairstylists, estheticians
    "622": "customer_service",       # Travel agents
    "623": "hospitality_food",       # Chefs, cooks
    "630": "hospitality_food",       # Food/beverage service workers (TEER 4)
    "631": "customer_service",       # Personal service workers (TEER 4)
    "640": "other",                  # Protective service workers (TEER 4)
    "650": "customer_service",       # Personal care workers (TEER 4/5)
    "700": "skilled_trades",         # Contractors/supervisors (TEER 0)
    "701": "skilled_trades",         # Managers in trades
    "720": "skilled_trades",         # Industrial, electrical, construction (TEER 2)
    "721": "skilled_trades",         # Electrical/electronics mechanics
    "722": "skilled_trades",         # Pipefitters, gas fitters
    "723": "skilled_trades",         # Machinists, metalworking
    "724": "skilled_trades",         # Woodworking
    "725": "skilled_trades",         # Printing occupations
    "726": "manufacturing",          # Other repair/maintenance
    "730": "skilled_trades",         # Construction trades (TEER 3)
    "731": "skilled_trades",         # Concrete, masonry
    "732": "skilled_trades",         # Carpentry
    "733": "skilled_trades",         # Roofing, siding, glazing
    "740": "skilled_trades",         # Other installers (TEER 3)
    "741": "skilled_trades",         # Appliance repair
    "742": "skilled_trades",         # Transport mechanics
    "750": "general_labor",          # Heavy equipment operators
    "751": "general_labor",          # Crane operators
    "752": "general_labor",          # Drillers
    "760": "general_labor",          # Transport truck drivers
    "761": "general_labor",          # Bus drivers
    "762": "general_labor",          # Delivery drivers
    "800": "general_labor",          # Natural resources supervisors
    "820": "general_labor",          # Agricultural workers
    "821": "general_labor",          # Livestock workers
    "823": "general_labor",          # Fishing workers
    "840": "general_labor",          # Mine workers
    "860": "general_labor",          # Harvesting labourers
    "900": "manufacturing",          # Processing supervisors (TEER 0)
    "920": "manufacturing",          # Textile/fabric workers
    "921": "manufacturing",          # Wood processors
    "922": "manufacturing",          # Pulp/paper
    "923": "manufacturing",          # Metal foundry
    "924": "manufacturing",          # Chemical plant
    "940": "manufacturing",          # Machine operators
    "941": "manufacturing",          # Food processors (TEER 4)
    "950": "manufacturing",          # Assemblers
    "951": "manufacturing",          # Electronic assemblers
    "960": "general_labor",          # Material handlers
    "961": "general_labor",          # Labourers
    # 4-digit NOC (old format) — first 3 digits as prefix
    # NOC 2016 broad categories 0-9 are first digit, so 3-digit prefix:
    "000": "business_operations",    # Senior management (old NOC)
    "011": "business_operations",    # Corporate management (old)
    "012": "finance_accounting",     # Insurance/financial management (old)
    "013": "business_operations",    # Govt managers (old)
    "014": "healthcare_admin",       # Health/education managers (old)
    "015": "other",                  # Managers in art/sport (old)
    "016": "business_operations",    # Other managers (old)
}

# 2-digit NOC prefix → role_function
_NOC2: dict[str, str] = {
    # 5-digit NOC 2021 (TEER + broad as first 2 digits)
    "00": "business_operations",   # TEER 0 + broad 0 = senior management
    "01": "business_operations",   # TEER 0 + broad 1 = financial/business execs
    "10": "business_operations",   # TEER 1 + broad 0 (corporate managers)
    "11": "marketing",             # TEER 1 + broad 1 (business/financial specialists)
    "12": "finance_accounting",    # TEER 1 + broad 2? / financial professionals
    "13": "business_operations",   # TEER 1 + broad 3 (admin support)
    "14": "business_operations",   # TEER 1 + broad 4
    "20": "research_science",      # TEER 2 + broad 0 (scientists)
    "21": "software_engineering",  # TEER 2 + broad 1 (tech professionals) — default
    "22": "hardware_engineering",  # TEER 2 + broad 2 (engineering technicians)
    "30": "clinical",              # TEER 3 + broad 0 (health practitioners)
    "31": "allied_health",         # TEER 3 + broad 1 (health professionals)
    "32": "allied_health",         # TEER 3 + broad 2 (health technicians)
    "33": "allied_health",         # TEER 3 + broad 3 (health assistants)
    "40": "legal",                 # TEER 4 + broad 0 (law/judges)
    "41": "teaching",              # TEER 4 + broad 1 (educators)
    "42": "other",                 # TEER 4 + broad 2 (social/community)
    "43": "other",                 # TEER 4 + broad 3 (protective service)
    "44": "customer_service",      # TEER 4 + broad 4 (care providers)
    "50": "design_ux",             # TEER 5 + broad 0 (art/culture)
    "51": "content_writing",       # TEER 5 + broad 1 (media/communication)
    "52": "retail",                # TEER 5 + broad 2 (retail supervisors)
    "53": "sales",                 # TEER 5 + broad 3 (sales representatives)
    "54": "customer_service",      # TEER 5 + broad 4 (service supervisors)
    "55": "hospitality_food",      # TEER 5 + broad 5 (food/hospitality)
    "60": "retail",                # TEER 6 + broad 0 (retail sales)
    "62": "hospitality_food",      # TEER 6 + broad 2 (food services)
    "63": "customer_service",      # TEER 6 + broad 3 (childcare/personal)
    "64": "other",                 # TEER 6 + broad 4 (protective service workers)
    "65": "customer_service",      # TEER 6 + broad 5 (personal care)
    "70": "skilled_trades",        # TEER 7 + broad 0 (contractors/supervisors)
    "72": "skilled_trades",        # TEER 7 + broad 2 (industrial trades)
    "73": "skilled_trades",        # TEER 7 + broad 3 (construction)
    "74": "skilled_trades",        # TEER 7 + broad 4 (maintenance)
    "75": "general_labor",         # TEER 7 + broad 5 (transport)
    "76": "general_labor",         # TEER 7 + broad 6 (heavy transport)
    "80": "general_labor",         # TEER 8 + broad 0 (natural resources)
    "82": "general_labor",         # TEER 8 + broad 2 (agriculture)
    "84": "general_labor",         # TEER 8 + broad 4 (mining)
    "86": "general_labor",         # TEER 8 + broad 6 (harvesting)
    "90": "manufacturing",         # TEER 9 + broad 0 (processing supervisors)
    "92": "manufacturing",         # TEER 9 + broad 2 (processors)
    "94": "manufacturing",         # TEER 9 + broad 4 (machine operators)
    "95": "manufacturing",         # TEER 9 + broad 5 (assemblers)
    "96": "general_labor",         # TEER 9 + broad 6 (material handlers)
}

# 1-digit NOC prefix → role_function
# (matches old NOC broad category as used by occupation_category.from_noc)
_NOC1: dict[str, str] = {
    "0": "business_operations",   # Management
    "1": "finance_accounting",    # Business, finance, admin
    "2": "software_engineering",  # Natural and applied sciences (default = tech)
    "3": "clinical",              # Health occupations (default = clinical)
    "4": "teaching",              # Education, law, social (default = teaching)
    "5": "design_ux",             # Art, culture, recreation (default = design)
    "6": "customer_service",      # Sales and service (default = customer_service)
    "7": "skilled_trades",        # Trades, transport (default = trades)
    "8": "general_labor",         # Natural resources (default = labour)
    "9": "manufacturing",         # Manufacturing and utilities
}


def _role_from_noc(noc_code: str) -> Optional[str]:
    """Return role_function from a NOC code, or None if unmappable.

    5-digit NOC 2021: exact → 3-digit prefix → 2-digit prefix → 1-digit.
    4-digit NOC 2016/2011: 1-digit only (the _NOC3 entries are calibrated for
    the 5-digit TEER-based structure and would give wrong results on old codes).
    """
    code = noc_code.strip()
    if not code or not code[:1].isdigit():
        return None
    # 5-digit NOC 2021: try exact → 3-digit → 2-digit → 1-digit
    if len(code) >= 5:
        c5 = code[:5]
        if c5 in _NOC5:
            return _NOC5[c5]
        c3 = code[:3]
        if c3 in _NOC3:
            return _NOC3[c3]
        c2 = code[:2]
        if c2 in _NOC2:
            return _NOC2[c2]
        return _NOC1.get(code[:1])
    # 4-digit old NOC: broad category is the first digit — use 1-digit only
    return _NOC1.get(code[:1])


# ---------------------------------------------------------------------------
# Tier 2: Title dictionary lookup
# ---------------------------------------------------------------------------

# Small hand-override map for common titles that O*NET misses or mis-classifies.
# Keys must already be normalised (lowercase, no punct, no seniority/level).
_HAND_OVERRIDES: dict[str, str] = {
    # tech — common titles O*NET doesn't carry verbatim
    "backend engineer": "software_engineering",
    "frontend engineer": "software_engineering",
    "frontend developer": "software_engineering",
    "backend developer": "software_engineering",
    "qa automation engineer": "qa",
    "cloud infrastructure engineer": "devops_sre",
    "embedded systems engineer": "hardware_engineering",
    "embedded software engineer": "hardware_engineering",
    "it support specialist": "it_support",
    # sdet
    "sdet": "qa",
    "software development engineer in test": "qa",
    "software engineer in test": "qa",
    "site reliability engineer": "devops_sre",
    "sre": "devops_sre",
    "platform engineer": "devops_sre",
    "infrastructure engineer": "devops_sre",
    "cloud engineer": "devops_sre",
    "devsecops engineer": "security",
    "application security engineer": "security",
    "security engineer": "security",
    "ml engineer": "data_ml",
    "machine learning engineer": "data_ml",
    "mlops engineer": "data_ml",
    "ai engineer": "data_ml",
    "data engineer": "data_ml",
    "analytics engineer": "data_analytics",
    "bi engineer": "data_analytics",
    "business intelligence engineer": "data_analytics",
    "data analyst": "data_analytics",
    "business analyst": "business_operations",
    "business intelligence analyst": "data_analytics",
    "business operations manager": "business_operations",
    "product analyst": "product_management",
    "growth engineer": "software_engineering",
    "full stack developer": "software_engineering",
    "full stack engineer": "software_engineering",
    "fullstack developer": "software_engineering",
    "fullstack engineer": "software_engineering",
    "backend developer": "software_engineering",
    "frontend developer": "software_engineering",
    "ios developer": "software_engineering",
    "android developer": "software_engineering",
    "mobile developer": "software_engineering",
    "embedded engineer": "hardware_engineering",
    "firmware engineer": "hardware_engineering",
    "member of technical staff": "software_engineering",
    "mts": "software_engineering",
    "software development engineer": "software_engineering",
    "sde": "software_engineering",
    "solutions architect": "software_engineering",
    "cloud architect": "devops_sre",
    "network engineer": "devops_sre",
    "network administrator": "devops_sre",
    "systems administrator": "devops_sre",
    "database administrator": "devops_sre",
    "dba": "devops_sre",
    "it administrator": "it_support",
    "helpdesk technician": "it_support",
    "help desk technician": "it_support",
    "it technician": "it_support",
    # product / project
    "product manager": "product_management",
    "product owner": "product_management",
    "scrum master": "project_program_management",
    "agile coach": "project_program_management",
    "program manager": "project_program_management",
    "project manager": "project_program_management",
    "delivery manager": "project_program_management",
    "technical program manager": "project_program_management",
    "tpm": "project_program_management",
    # sales / business
    "account executive": "sales",
    "ae": "sales",
    "account manager": "sales",
    "business development representative": "sales",
    "bdr": "sales",
    "sdr": "sales",
    "sales development representative": "sales",
    "customer success manager": "customer_success",
    "csm": "customer_success",
    "customer success": "customer_success",
    "vp of sales": "sales",
    "vp sales": "sales",
    "chief revenue officer": "sales",
    "cro": "sales",
    "growth manager": "marketing",
    "growth marketing manager": "marketing",
    "demand generation": "marketing",
    "seo specialist": "marketing",
    # management titles that deserve specific classification
    "cto": "software_engineering",
    "chief technology officer": "software_engineering",
    "cpo": "product_management",
    "chief product officer": "product_management",
    "vp of engineering": "software_engineering",
    "vp engineering": "software_engineering",
    "director of engineering": "software_engineering",
    "engineering manager": "software_engineering",
    "head of engineering": "software_engineering",
    "head of product": "product_management",
    "director of product": "product_management",
    # healthcare
    "rn": "nursing",
    "registered nurse": "nursing",
    "lpn": "nursing",
    "np": "clinical",
    "nurse practitioner": "clinical",
    "pa": "clinical",
    "physician assistant": "clinical",
    "md": "clinical",
    "physician": "clinical",
    "hospitalist": "clinical",
    "intensivist": "clinical",
    "paramedic": "allied_health",
    "emt": "allied_health",
    "pharmacy technician": "allied_health",
    "phlebotomist": "lab_technician",
    # creative
    "ux designer": "design_ux",
    "ui designer": "design_ux",
    "product designer": "design_ux",
    "ux researcher": "design_ux",
    "visual designer": "design_ux",
    "brand designer": "design_ux",
    "motion designer": "media_production",
    "copywriter": "content_writing",
    "technical writer": "content_writing",
    "content strategist": "content_writing",
    # service / hospitality
    "barista": "hospitality_food",
    "bartender": "hospitality_food",
    "cook": "hospitality_food",
    "chef": "hospitality_food",
    "line cook": "hospitality_food",
    "server": "hospitality_food",
    "waiter": "hospitality_food",
    "waitress": "hospitality_food",
    "cashier": "retail",
    "retail associate": "retail",
    "store associate": "retail",
    # education
    "teacher": "teaching",
    "professor": "teaching",
    "lecturer": "teaching",
    "instructor": "teaching",
    "tutor": "teaching",
    "principal": "education_admin",
    "school counselor": "education_admin",
    # finance
    "accountant": "finance_accounting",
    "bookkeeper": "finance_accounting",
    "financial analyst": "finance_accounting",
    "investment analyst": "finance_accounting",
    "cfo": "finance_accounting",
    "chief financial officer": "finance_accounting",
    # hr
    "recruiter": "hr_recruiting",
    "talent acquisition": "hr_recruiting",
    "hr business partner": "hr_recruiting",
    "hrbp": "hr_recruiting",
    "people operations": "hr_recruiting",
    # legal
    "lawyer": "legal",
    "attorney": "legal",
    "paralegal": "legal",
    "legal counsel": "legal",
    # science / research
    "research scientist": "research_science",
    "scientist": "research_science",
    "lab technician": "lab_technician",
    "laboratory technician": "lab_technician",
    "research assistant": "lab_technician",
}


@lru_cache(maxsize=1)
def _load_lookup() -> dict[str, str]:
    """Load role_title_lookup.json once and cache it."""
    p = Path(__file__).parent.parent / "data" / "role_title_lookup.json"
    if not p.exists():
        logger.warning("role_title_lookup.json not found at %s; dict tier disabled", p)
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# Title normalisation (mirrors build_role_dictionary.py)
_SENIORITY_RE = re.compile(
    r"\b(senior|sr\.?|junior|jr\.?|lead|principal|"
    r"associate|head\s+of|entry[\s\-]?level|mid[\s\-]?level|"
    r"experienced|distinguished|fellow|founding|interim|acting)\b"
    # "staff" only as a seniority prefix: "Staff Engineer" strips it;
    # "Member of Technical Staff" (end of string) does not.
    r"|\bstaff(?=\s)",
    re.IGNORECASE,
)
_LEVEL_SUFFIX_RE = re.compile(r"\b(i{1,3}v?|[1-4])\s*$", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _normalize(title: str) -> str:
    t = title.lower().strip()
    t = _SENIORITY_RE.sub("", t)
    t = _LEVEL_SUFFIX_RE.sub("", t)
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def _dict_lookup(title: str) -> Optional[str]:
    """Check hand-overrides then O*NET-derived lookup.

    After an exact-match attempt, slides a window of decreasing length over
    the normalised token sequence so that compound real titles like
    "Senior Data Analyst, Strategic Health Analytics" (normalised to
    "data analyst strategic health analytics") still resolve via the
    longest matching sub-phrase ("data analyst" → data_analytics).
    """
    key = _normalize(title)
    if not key:
        return None
    if key in _HAND_OVERRIDES:
        return _HAND_OVERRIDES[key]
    lookup = _load_lookup()
    if key in lookup:
        return lookup[key]
    tokens = key.split()
    for n in range(len(tokens), 0, -1):
        for i in range(len(tokens) - n + 1):
            phrase = " ".join(tokens[i:i + n])
            # Single-token sub-phrase: only trust hand overrides (curated).
            # The O*NET dict has many generic single words ("researcher", "analyst")
            # that produce false positives on compound titles like "BRAIN Researcher".
            if n == 1:
                hit = _HAND_OVERRIDES.get(phrase)
            else:
                hit = _HAND_OVERRIDES.get(phrase) or lookup.get(phrase)
            if hit:
                return hit
    return None


# ---------------------------------------------------------------------------
# Tier 3: LLM fallback
#
# Provider is auto-detected from ROLE_LLM_MODEL:
#   gpt-*     → OpenAI
#   claude-*  → Anthropic
#   default   → OpenAI
#
# To swap providers: change only this function.
# If ROLE_LLM_API_KEY is absent the function returns None so the module works
# offline (tests, CI without credentials).
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = (
    "You are a job-title classifier. Given a job title, return ONLY a JSON object "
    "with a single key 'role_function' whose value is exactly one of these strings:\n"
    "software_engineering, data_ml, data_analytics, devops_sre, security, qa, "
    "it_support, hardware_engineering, product_management, project_program_management, "
    "sales, marketing, customer_success, finance_accounting, hr_recruiting, legal, "
    "business_operations, consulting, clinical, nursing, allied_health, healthcare_admin, "
    "design_ux, content_writing, media_production, research_science, lab_technician, "
    "teaching, education_admin, skilled_trades, manufacturing, hospitality_food, retail, "
    "customer_service, general_labor, other\n"
    "Return ONLY the JSON. No explanation."
)


def llm_classify(title: str) -> Optional[dict]:
    """Call the configured LLM to classify a title.

    Returns {"role_function": "<value>"} or None if key absent / call fails.
    To swap providers, edit this function only.
    """
    api_key = os.environ.get("ROLE_LLM_API_KEY", "").strip()
    if not api_key:
        return None

    model = os.environ.get("ROLE_LLM_MODEL", "gpt-4o-mini").strip()

    try:
        if model.startswith("claude"):
            return _llm_anthropic(title, model, api_key)
        return _llm_openai(title, model, api_key)
    except Exception as exc:
        logger.warning("LLM classification failed for %r: %s", title, exc)
        return None


def _llm_openai(title: str, model: str, api_key: str) -> Optional[dict]:
    try:
        import openai  # noqa: PLC0415
    except ImportError:
        logger.warning("openai package not installed; LLM tier disabled")
        return None

    client = openai.OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": title},
        ],
        max_tokens=30,
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or ""
    return _parse_llm_response(raw)


def _llm_anthropic(title: str, model: str, api_key: str) -> Optional[dict]:
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        logger.warning("anthropic package not installed; LLM tier disabled")
        return None

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=30,
        system=_LLM_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": title}],
    )
    raw = msg.content[0].text if msg.content else ""
    return _parse_llm_response(raw)


def _parse_llm_response(raw: str) -> Optional[dict]:
    try:
        data = json.loads(raw.strip())
        rf = data.get("role_function", "").strip()
        if rf in VALID_FUNCTIONS:
            return {"role_function": rf}
        logger.warning("LLM returned unknown role_function: %r", rf)
        return None
    except json.JSONDecodeError:
        logger.warning("LLM response not valid JSON: %r", raw[:200])
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_role(
    title: str,
    noc_code: Optional[str] = None,
) -> dict:
    """Classify a job title's role_function via a cheap-first cascade.

    Args:
        title:    Job title string.
        noc_code: Canadian NOC code (4 or 5 digits), optional.

    Returns:
        {
          "role_function": str,   # one of VALID_FUNCTIONS
          "method":        str,   # "noc" | "dict" | "llm" | "fallback"
          "confidence":    float, # 0.0-1.0 (rough indicator)
        }
    """
    # Tier 1: NOC code
    if noc_code:
        rf = _role_from_noc(noc_code)
        if rf and rf in VALID_FUNCTIONS:
            return {"role_function": rf, "method": "noc", "confidence": 0.95}

    # Tier 2: dictionary
    if title:
        rf = _dict_lookup(title)
        if rf and rf in VALID_FUNCTIONS:
            return {"role_function": rf, "method": "dict", "confidence": 0.85}

    # Tier 3: LLM
    if title:
        result = llm_classify(title)
        if result:
            rf = result.get("role_function", "")
            if rf in VALID_FUNCTIONS:
                return {"role_function": rf, "method": "llm", "confidence": 0.75}

    return {"role_function": "other", "method": "fallback", "confidence": 0.0}
