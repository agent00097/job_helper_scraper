"""
Unit tests for utils.company_normalization.normalize_company_name.

Run from repo root:
    python tests/test_company_normalization.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.company_normalization import normalize_company_name


CASES = [
    # (input, expected)
    # --- Trailing Inc / LLC / Corp ---
    ("Stripe, Inc.",             "stripe"),
    ("Stripe Inc",               "stripe"),
    ("Stripe Inc.",              "stripe"),
    ("Google LLC",               "google"),
    ("Google, LLC",              "google"),
    ("OpenAI, L.L.C.",           "openai"),
    ("OpenAI L.L.C.",            "openai"),
    ("Microsoft Corporation",    "microsoft"),
    ("Microsoft Corp.",          "microsoft"),
    ("Microsoft Corp",           "microsoft"),
    ("Acme Corporation",         "acme"),
    ("Shopify Inc",              "shopify"),
    ("Amazon.com, Inc.",         "amazon.com"),   # domain-style name preserved
    # --- Ltd / Limited ---
    ("Tata Consultancy Services Limited", "tata consultancy services"),
    ("Rolls-Royce Holdings PLC", "rolls-royce holdings"),
    ("HSBC Holdings plc",        "hsbc holdings"),
    ("Acme Co., Ltd.",           "acme"),
    ("Acme Co., Limited",        "acme"),
    # --- GmbH / AG / SA / S.A. ---
    ("Siemens AG",               "siemens"),
    ("Deutsche Bank AG",         "deutsche bank"),
    ("Nestlé S.A.",              "nestlé"),
    ("Nestlé SA",                "nestlé"),
    ("SAP SE",                   "sap se"),          # SE not in suffix list — keep
    ("Bosch GmbH",               "bosch"),
    # --- Company / Co. ---
    ("Procter & Gamble Company", "procter & gamble"),
    ("Ford Motor Co.",           "ford motor"),
    ("Berkshire Hathaway Inc.",  "berkshire hathaway"),
    # --- No suffix — untouched (other than case + whitespace) ---
    ("Amazon",                   "amazon"),
    ("Shopify",                  "shopify"),
    ("Lyft",                     "lyft"),
    ("Cisco Systems",            "cisco systems"),
    # --- Whitespace normalisation ---
    ("  Apple  Inc.  ",          "apple"),
    ("Twitter,  Inc.",           "twitter"),
    # --- Already normalised ---
    ("stripe",                   "stripe"),
    ("",                         ""),
]


def run_tests():
    passed = 0
    failed = 0
    for raw, expected in CASES:
        result = normalize_company_name(raw)
        if result == expected:
            passed += 1
        else:
            print(f"FAIL  {raw!r:45s}  got={result!r}  want={expected!r}")
            failed += 1

    print(f"\n{passed}/{passed + failed} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
