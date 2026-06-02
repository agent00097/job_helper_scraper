"""
Unit tests for utils.occupation_category.from_noc and from_title.

Run from repo root:
    python tests/test_occupation_category.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.occupation_category import from_noc, from_title


# ---------------------------------------------------------------------------
# from_noc
# ---------------------------------------------------------------------------

NOC_CASES = [
    # (input, expected)
    ("0111",  "business"),    # 0-prefix → business (management)
    ("1111",  "business"),    # 1-prefix → business (admin/finance)
    ("21221", "tech"),        # 2-prefix → tech
    ("31301", "healthcare"),  # 3-prefix → healthcare
    ("4112",  "education"),   # 4-prefix → education
    ("5131",  "creative"),    # 5-prefix → creative
    ("6211",  "service"),     # 6-prefix → service
    ("7301",  "trades"),      # 7-prefix → trades
    ("8411",  "trades"),      # 8-prefix → trades (primary)
    ("9461",  "trades"),      # 9-prefix → trades (processing/manufacturing)
    # Edge cases
    ("",      "other"),
    (None,    "other"),       # type: ignore
    ("abc",   "other"),       # non-digit first char
    ("99999", "trades"),      # valid 5-digit
]


# ---------------------------------------------------------------------------
# from_title
# ---------------------------------------------------------------------------

TITLE_CASES = [
    # Tech
    ("Software Engineer",              "tech"),
    ("Senior Software Engineer",       "tech"),
    ("Backend Developer",              "tech"),
    ("Frontend Developer",             "tech"),
    ("Full Stack Developer",           "tech"),
    ("Full-Stack Engineer",            "tech"),
    ("DevOps Engineer",                "tech"),
    ("Site Reliability Engineer",      "tech"),   # "sre" substring not present but "engineer" is
    ("SRE",                            "tech"),
    ("Data Scientist",                 "tech"),   # tech, NOT science
    ("ML Engineer",                    "tech"),
    ("Machine Learning Engineer",      "tech"),
    ("Data Engineer",                  "tech"),
    ("Platform Engineer",              "tech"),
    ("Security Engineer",              "tech"),   # explicit tech phrase
    ("iOS Developer",                  "tech"),
    ("Android Engineer",               "tech"),
    ("QA Engineer",                    "tech"),
    ("SDET",                           "tech"),
    ("Infrastructure Engineer",        "tech"),
    ("Mobile Engineer",                "tech"),
    ("Cloud Engineer",                 "tech"),
    # --- "engineer" exclusions (non-tech disciplines) ---
    ("Sales Engineer",                 "business"),  # sales → business before engineer fallback
    ("Service Engineer",               "other"),     # excluded from tech, no other match
    # Healthcare
    ("Registered Nurse",               "healthcare"),
    ("Nurse Practitioner",             "healthcare"),
    ("Physician",                      "healthcare"),
    ("Family Doctor",                  "healthcare"),
    ("Clinical Pharmacist",            "healthcare"),
    ("Physiotherapist",                "healthcare"),
    ("Dentist",                        "healthcare"),
    ("Medical Laboratory Technician",  "healthcare"),
    # Business
    ("Account Executive",              "business"),
    ("Sales Representative",           "business"),
    ("Marketing Manager",              "business"),
    ("Finance Analyst",                "business"),
    ("Accounting Manager",             "business"),
    ("HR Business Partner",            "business"),
    ("Recruiter",                      "business"),
    ("Operations Manager",             "business"),
    ("Product Manager",                "business"),
    ("Business Analyst",               "business"),
    ("Customer Success Manager",       "business"),
    # Education
    ("High School Teacher",            "education"),
    ("Professor of Biology",           "education"),
    ("Instructor",                     "education"),
    ("Math Tutor",                     "education"),
    ("Educator",                       "education"),
    # Creative
    ("UX Designer",                    "creative"),
    ("UI/UX Designer",                 "creative"),
    ("Graphic Designer",               "creative"),
    ("Copywriter",                     "creative"),
    ("Content Writer",                 "creative"),
    ("Brand Manager",                  "creative"),
    ("Illustrator",                    "creative"),
    ("Photographer",                   "creative"),
    ("Video Editor",                   "creative"),
    # Science (NOT 'data scientist' — that's tech)
    ("Research Scientist",             "science"),
    ("Researcher",                     "science"),
    ("Biologist",                      "science"),
    ("Chemist",                        "science"),
    ("Physicist",                      "science"),
    ("Lab Technician",                 "science"),   # "lab tech" matches
    # Service
    ("Bartender",                      "service"),
    ("Cashier",                        "service"),
    ("Retail Associate",               "service"),
    ("Waiter",                         "service"),
    ("Waitress",                       "service"),
    ("Chef",                           "service"),
    ("Cook",                           "service"),
    ("Housekeeping",                   "service"),
    ("Hospitality Manager",            "service"),
    # Trades
    ("Carpenter",                      "trades"),
    ("Electrician",                    "trades"),
    ("Plumber",                        "trades"),
    ("Welder",                         "trades"),
    ("Truck Driver",                   "trades"),
    ("Auto Mechanic",                  "trades"),
    ("Construction Worker",            "trades"),
    ("Roofer",                         "trades"),
    ("Delivery Driver",                "trades"),
    # Fallback
    ("",                               "other"),
    ("Intern",                         "other"),
    ("Administrative Assistant",       "other"),
]


def run_noc_tests():
    passed = 0
    failed = 0
    for code, expected in NOC_CASES:
        result = from_noc(code)  # type: ignore[arg-type]
        if result == expected:
            passed += 1
        else:
            print(f"FAIL  from_noc({code!r:8s})  got={result!r}  want={expected!r}")
            failed += 1
    return passed, failed


def run_title_tests():
    passed = 0
    failed = 0
    for title, expected in TITLE_CASES:
        result = from_title(title)
        if result == expected:
            passed += 1
        else:
            print(f"FAIL  from_title({title!r:40s})  got={result!r}  want={expected!r}")
            failed += 1
    return passed, failed


def run_tests():
    np, nf = run_noc_tests()
    tp, tf = run_title_tests()

    total_p = np + tp
    total_f = nf + tf
    print(f"\nfrom_noc:   {np}/{np + nf} passed")
    print(f"from_title: {tp}/{tp + tf} passed")
    print(f"Total:      {total_p}/{total_p + total_f} passed")
    if total_f:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
