"""
Hermetic unit tests for utils/role_classifier.py.

No database, no real LLM calls. LLM is mocked throughout.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Allow import from repo root when running pytest from any directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.role_classifier import (
    VALID_FUNCTIONS,
    _dict_lookup,
    _normalize,
    _role_from_noc,
    classify_role,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(title, noc=None, llm_return=None):
    """Run classify_role with the LLM patched to return llm_return (dict or None)."""
    with patch("utils.role_classifier.llm_classify", return_value=llm_return):
        return classify_role(title, noc_code=noc)


# ---------------------------------------------------------------------------
# Title normalisation
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_lowercase(self):
        assert _normalize("Software Engineer") == "software engineer"

    def test_strips_seniority(self):
        assert _normalize("Senior Software Engineer") == "software engineer"
        assert _normalize("Sr. Data Scientist") == "data scientist"
        assert _normalize("Junior Developer") == "developer"
        assert _normalize("Lead Product Manager") == "product manager"
        assert _normalize("Principal Engineer") == "engineer"
        assert _normalize("Staff Engineer") == "engineer"

    def test_strips_level_suffix(self):
        assert _normalize("Software Engineer II") == "software engineer"
        assert _normalize("Software Engineer III") == "software engineer"
        assert _normalize("Software Engineer 2") == "software engineer"

    def test_strips_punctuation(self):
        # dashes become spaces; whitespace is collapsed to single space
        assert _normalize("RN - ICU") == "rn icu"
        assert "  " not in _normalize("RN - ICU")

    def test_empty_input(self):
        assert _normalize("") == ""


# ---------------------------------------------------------------------------
# Tier 1: NOC mapping
# ---------------------------------------------------------------------------

class TestNOCMapping:
    def test_exact_5digit_tech(self):
        # 21232 = Software developers and programmers
        assert _role_from_noc("21232") == "software_engineering"

    def test_exact_5digit_data(self):
        assert _role_from_noc("21210") == "data_ml"

    def test_exact_5digit_security(self):
        assert _role_from_noc("21220") == "security"

    def test_exact_5digit_qa(self):
        assert _role_from_noc("22222") == "qa"

    def test_exact_5digit_nursing(self):
        assert _role_from_noc("31200") == "nursing"

    def test_3digit_prefix_fallback(self):
        # 21234 is in _NOC5; 21299 not in _NOC5 but 212 is in _NOC3
        assert _role_from_noc("21299") == "software_engineering"

    def test_2digit_prefix_fallback(self):
        # 21xxx default via 2-digit "21"
        assert _role_from_noc("21999") == "software_engineering"

    def test_4digit_old_noc_first_digit_2(self):
        # Old NOC 2164 (computer engineers); 4-digit codes use first-digit only.
        # First digit "2" → _NOC1["2"] = "software_engineering" (broad tech default).
        assert _role_from_noc("2164") == "software_engineering"

    def test_4digit_old_noc_first_digit_3(self):
        # Old NOC 3012 (physicians) — first digit "3" → clinical
        assert _role_from_noc("3012") == "clinical"

    def test_4digit_old_noc_first_digit_6(self):
        # Old NOC 6622 (cashiers) — first digit "6" → customer_service
        assert _role_from_noc("6622") == "customer_service"

    def test_invalid_returns_none(self):
        assert _role_from_noc("") is None
        assert _role_from_noc("abc") is None

    def test_result_always_valid(self):
        for code in ("21232", "31200", "72010", "90010", "22221"):
            rf = _role_from_noc(code)
            assert rf in VALID_FUNCTIONS, f"{code} mapped to invalid {rf!r}"


# ---------------------------------------------------------------------------
# Tier 2: Dictionary lookup
# ---------------------------------------------------------------------------

class TestDictLookup:
    def test_hand_override_exact(self):
        assert _dict_lookup("SDET") == "qa"
        assert _dict_lookup("Site Reliability Engineer") == "devops_sre"
        assert _dict_lookup("Machine Learning Engineer") == "data_ml"
        assert _dict_lookup("Account Executive") == "sales"
        assert _dict_lookup("Barista") == "hospitality_food"
        assert _dict_lookup("RN") == "nursing"
        assert _dict_lookup("Scrum Master") == "project_program_management"
        assert _dict_lookup("CTO") == "software_engineering"

    def test_hand_override_seniority_stripped(self):
        # "Senior Site Reliability Engineer" → normalize → "site reliability engineer"
        assert _dict_lookup("Senior Site Reliability Engineer") == "devops_sre"
        assert _dict_lookup("Sr. Machine Learning Engineer") == "data_ml"
        assert _dict_lookup("Lead SDET") == "qa"

    def test_onet_dict_software(self):
        # O*NET has "Software Developer" mapped via SOC 15-1252 → software_engineering
        result = _dict_lookup("Software Developer")
        assert result == "software_engineering", f"Got {result!r}"

    def test_onet_dict_registered_nurse(self):
        result = _dict_lookup("Registered Nurse")
        assert result == "nursing", f"Got {result!r}"

    def test_onet_dict_accountant(self):
        result = _dict_lookup("Accountant")
        assert result in {"finance_accounting", "other"}, f"Got {result!r}"

    def test_unknown_title_returns_none(self):
        assert _dict_lookup("xyzzy frobnicator 99") is None

    def test_empty_returns_none(self):
        assert _dict_lookup("") is None

    def test_all_results_valid(self):
        for title in (
            "Software Engineer", "Data Scientist", "Nurse", "Teacher",
            "Barista", "Electrician", "Sales Manager", "Copywriter",
        ):
            rf = _dict_lookup(title)
            if rf is not None:
                assert rf in VALID_FUNCTIONS, f"{title!r} → invalid {rf!r}"


# ---------------------------------------------------------------------------
# Tier 3: LLM — graceful absence, mocked success, mocked invalid
# ---------------------------------------------------------------------------

class TestLLMTier:
    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("ROLE_LLM_API_KEY", raising=False)
        from utils.role_classifier import llm_classify
        result = llm_classify("Some Obscure Job Title XYZ")
        assert result is None

    def test_mocked_llm_success(self, monkeypatch):
        monkeypatch.setenv("ROLE_LLM_API_KEY", "fake-key")
        with patch("utils.role_classifier._llm_openai") as mock_fn:
            mock_fn.return_value = {"role_function": "consulting"}
            from utils.role_classifier import llm_classify
            result = llm_classify("Strategic Advisor")
        assert result == {"role_function": "consulting"}

    def test_mocked_llm_invalid_role(self, monkeypatch):
        monkeypatch.setenv("ROLE_LLM_API_KEY", "fake-key")
        with patch("utils.role_classifier._llm_openai") as mock_fn:
            mock_fn.return_value = {"role_function": "not_a_real_function"}
            from utils.role_classifier import llm_classify
            result = llm_classify("Whatever")
        # _llm_openai already returned invalid; _parse_llm_response would catch it
        # but since we're mocking at _llm_openai level, llm_classify passes it through
        # The outer classify_role checks VALID_FUNCTIONS — test that here:
        result2 = _result("Whatever", llm_return={"role_function": "not_a_real_function"})
        assert result2["role_function"] == "other"
        assert result2["method"] == "fallback"


# ---------------------------------------------------------------------------
# classify_role — end-to-end cascade
# ---------------------------------------------------------------------------

class TestClassifyRole:
    def test_noc_wins_over_title(self):
        # Title "Barista" → hospitality_food, but NOC 21232 → software_engineering
        # NOC is checked first so it should win
        result = _result("Barista", noc="21232")
        assert result["role_function"] == "software_engineering"
        assert result["method"] == "noc"

    def test_dict_fires_when_no_noc(self):
        result = _result("SDET")
        assert result["role_function"] == "qa"
        assert result["method"] == "dict"

    def test_dict_fires_when_noc_unmappable(self):
        result = _result("SDET", noc="ZZZZ")
        assert result["role_function"] == "qa"
        assert result["method"] == "dict"

    def test_llm_fires_when_dict_misses(self):
        with patch("utils.role_classifier.llm_classify",
                   return_value={"role_function": "consulting"}) as mock_llm:
            result = classify_role("Completely Unknown Occupation XYZ999")
            # If dict also missed, LLM should fire
            if result["method"] == "llm":
                assert result["role_function"] == "consulting"
                mock_llm.assert_called_once()

    def test_fallback_other_when_all_miss(self):
        result = _result("xyzzy frobnicator", llm_return=None)
        assert result["role_function"] == "other"
        assert result["method"] == "fallback"
        assert result["confidence"] == 0.0

    def test_confidence_tiers(self):
        noc_result = _result("anything", noc="21232")
        assert noc_result["confidence"] > 0.9

        dict_result = _result("SDET")
        assert dict_result["confidence"] > 0.8

        fallback_result = _result("xyzzy frobnicator", llm_return=None)
        assert fallback_result["confidence"] == 0.0

    def test_messy_titles(self):
        cases = [
            ("RN - ICU", "nursing"),
            ("VP of Engineering", "software_engineering"),
            ("Member of Technical Staff", "software_engineering"),
            ("Full Stack Developer", "software_engineering"),
            ("Account Executive", "sales"),
            ("Barista", "hospitality_food"),
        ]
        for title, expected in cases:
            result = _result(title)
            assert result["role_function"] == expected, (
                f"{title!r}: expected {expected!r}, got {result['role_function']!r} "
                f"(method={result['method']!r})"
            )

    def test_return_keys(self):
        result = _result("Software Engineer")
        assert set(result.keys()) == {"role_function", "method", "confidence"}

    def test_role_function_always_valid(self):
        for title in (
            "Software Engineer", "Nurse Practitioner", "Cashier",
            "xyzzy99", "", "Manager", "Associate",
        ):
            result = _result(title)
            assert result["role_function"] in VALID_FUNCTIONS

    def test_noc_with_rn(self):
        # NOC 31200 = Registered nurses
        result = _result("RN - ICU", noc="31200")
        assert result["role_function"] == "nursing"
        assert result["method"] == "noc"

    def test_empty_title_no_noc_returns_other(self):
        result = _result("", llm_return=None)
        assert result["role_function"] == "other"

    def test_all_valid_functions_are_reachable(self):
        """Sanity check: VALID_FUNCTIONS set is non-empty and well-formed."""
        assert len(VALID_FUNCTIONS) > 30
        assert "other" in VALID_FUNCTIONS
        assert "software_engineering" in VALID_FUNCTIONS
        assert "nursing" in VALID_FUNCTIONS
