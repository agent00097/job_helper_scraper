"""resolve_dead_boards: finding where a company with a dead board hires now.

All HTTP is mocked: a fake router maps URLs to (status, json/text).
"""
from unittest.mock import patch

import pytest

from utils import board_finder as rdb


class FakeResp:
    def __init__(self, status=200, data=None, text="", url=""):
        self.status_code = status
        self._data = data
        self.text = text
        self.url = url

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def router(routes):
    """routes: {url_prefix: FakeResp}; longest matching prefix wins, else 404."""
    def fake_get(url, params=None, **_kw):
        full = url
        if params:
            full += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        for prefix in sorted(routes, key=len, reverse=True):
            if full.startswith(prefix):
                resp = routes[prefix]
                if not resp.url:
                    resp.url = url
                return resp
        return FakeResp(404, url=url)
    return fake_get


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("utils.board_finder.time.sleep"):
        yield


def run(row, routes, guess_domain=False):
    with patch.object(rdb.SESSION, "get", side_effect=router(routes)):
        return rdb.resolve_one(row, guess_domain=guess_domain)


# ---------------------------------------------------------------- helpers
def test_name_matching():
    assert rdb.name_matches("Visier Solutions Inc", "Visier")
    assert rdb.name_matches("Runpod, Inc.", "RunPod")
    assert not rdb.name_matches("Loop", "Loopio")          # too short to prefix-match
    assert not rdb.name_matches("Temporal", "Tempo")
    assert rdb.mentioned_in("Wayve", ["At Wayve we build embodied AI."])
    assert not rdb.mentioned_in("Sa", ["sa sa sa"])        # too short to trust


def test_slug_candidates():
    c = rdb.slug_candidates("Hubspot", "hubspotjobs", None)
    assert c[0] == "hubspotjobs" and "hubspot" in c
    assert "verkada" in rdb.slug_candidates("Verkada2", "verkada2", None)
    assert "fivetran" in rdb.slug_candidates("dbt Labs", "dbtlabsinc", "https://www.fivetran.com")
    assert "visier" in rdb.slug_candidates("Visier Solutions Inc", "visiersolutionsinc", None)


def test_find_ats_links():
    html = """
      <a href="https://jobs.ashbyhq.com/acme">Jobs</a>
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeco"></script>
      <a href="https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers/job/123">x</a>
      <a href="https://careers-acme.icims.com/jobs">US</a>
    """
    supported, unsupported = rdb.find_ats_links(html)
    assert ("ashby", "acme") in supported
    assert ("greenhouse", "acmeco") in supported
    assert ("greenhouse", "embed") not in supported
    assert ("workday", "https://acme.wd5.myworkdayjobs.com/AcmeCareers") in supported
    assert unsupported == ["icims"]


# ---------------------------------------------------------------- resolve_one
def test_moved_to_ashby_same_slug_confirmed_by_job_text():
    routes = {
        "https://api.ashbyhq.com/posting-api/job-board/wayve": FakeResp(200, {
            "jobs": [{"descriptionPlain": "Wayve is building embodied AI for driving."}],
        }),
    }
    res = run({"company_name": "Wayve", "source": "greenhouse", "old_endpoint": "wayve"}, routes)
    assert res["status"] == "relocated"
    assert (res["new_source"], res["new_endpoint"]) == ("ashby", "wayve")
    assert res["confidence"] == "high"
    assert res["jobs"] == "1"


def test_new_greenhouse_token_confirmed_by_board_name():
    routes = {
        "https://boards-api.greenhouse.io/v1/boards/hubspot/jobs": FakeResp(200, {"jobs": [{"company_name": "HubSpot"}] * 3}),
        "https://boards-api.greenhouse.io/v1/boards/hubspot": FakeResp(200, {"name": "HubSpot"}),
    }
    res = run({"company_name": "Hubspot", "source": "greenhouse", "old_endpoint": "hubspotjobs"}, routes)
    assert (res["new_source"], res["new_endpoint"], res["confidence"]) == ("greenhouse", "hubspot", "high")
    assert res["jobs"] == "3"


def test_dead_board_itself_is_never_returned():
    # The old greenhouse slug "answering" again must not count as a relocation.
    routes = {
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs": FakeResp(200, {"jobs": []}),
        "https://boards-api.greenhouse.io/v1/boards/acme": FakeResp(200, {"name": "Acme"}),
    }
    res = run({"company_name": "Acme", "source": "greenhouse", "old_endpoint": "acme"}, routes)
    assert res["status"] == "not_found"


def test_slug_only_match_is_not_high_confidence():
    routes = {
        "https://api.lever.co/v0/postings/russett?mode=json": FakeResp(200, [{"descriptionPlain": "We are hiring."}]),
    }
    res = run({"company_name": "Russett", "source": "greenhouse", "old_endpoint": "russett"}, routes)
    assert res["status"] == "relocated"
    assert res["confidence"] == "medium"


def test_wrong_company_on_smartrecruiters_is_low_confidence():
    routes = {
        "https://api.smartrecruiters.com/v1/companies/loop/postings?limit=5": FakeResp(200, {
            "totalFound": 4, "content": [{"company": {"name": "Loop Industries"}}],
        }),
    }
    res = run({"company_name": "Loop", "source": "greenhouse", "old_endpoint": "loop"}, routes)
    assert res["confidence"] == "low"


def test_careers_page_unsupported_ats():
    routes = {
        "https://github.com/careers": FakeResp(200, text='<a href="https://careers-githubinc.icims.com/jobs">Jobs</a>'),
    }
    res = run({"company_name": "GitHub", "source": "greenhouse", "old_endpoint": "github", "domain": "github.com"}, routes)
    assert res["status"] == "unsupported_ats"
    assert res["new_source"] == "icims"


def test_careers_page_link_to_supported_ats_is_high():
    routes = {
        "https://acme.io/careers": FakeResp(200, text='<a href="https://jobs.lever.co/acme-hq">Open roles</a>'),
        "https://api.lever.co/v0/postings/acme-hq?mode=json": FakeResp(200, [{"descriptionPlain": "Join us"}] * 2),
    }
    res = run({"company_name": "Zeta Thing", "source": "greenhouse", "old_endpoint": "zetathing", "domain": "acme.io"}, routes)
    assert (res["new_source"], res["new_endpoint"], res["confidence"]) == ("lever", "acme-hq", "high")


def test_acquisition_redirect_tries_new_owner_slug():
    routes = {
        "https://getdbt.com/careers": FakeResp(200, text="<p>Join us</p><a href='?gh_jid=1'>x</a>", url="https://www.fivetran.com/careers"),
        "https://boards-api.greenhouse.io/v1/boards/fivetran/jobs": FakeResp(200, {"jobs": [{"company_name": "Fivetran"}]}),
        "https://boards-api.greenhouse.io/v1/boards/fivetran": FakeResp(200, {"name": "Fivetran"}),
    }
    # find_ats_links finds nothing, so the redirect host is the only lead.
    with patch.object(rdb, "find_ats_links", return_value=([], [])), \
         patch.object(rdb, "scan_careers", return_value=([], [], "https://www.fivetran.com/careers")):
        res = run({"company_name": "dbt Labs", "source": "greenhouse", "old_endpoint": "dbtlabsinc", "domain": "getdbt.com"}, routes)
    assert (res["new_source"], res["new_endpoint"]) == ("greenhouse", "fivetran")
    assert "redirects to fivetran.com" in res["evidence"]
    assert res["confidence"] != "high"  # different company name: a human confirms


def test_nothing_found_and_errors_are_reported():
    res = run({"company_name": "Ghost Co", "source": "greenhouse", "old_endpoint": "ghostco"}, {})
    assert res["status"] == "not_found"
    assert "no domain known" in res["evidence"]

    with patch.object(rdb, "slug_candidates", side_effect=RuntimeError("boom")):
        res = rdb.resolve_one({"company_name": "X", "source": "greenhouse", "old_endpoint": "x"})
    assert res["status"] == "error"




def test_prefix_match_only_drops_generic_words():
    assert rdb.name_matches("Visier Solutions Inc", "Visier")
    assert not rdb.name_matches("Galileo Financial Technologies", "Galileo")
    assert not rdb.name_matches("Remedy Meds", "Remedy")
    assert not rdb.name_matches("Found Ee", "Found")


@pytest.mark.parametrize("name,slug", [
    ("LI Test Company", "linkedin"),
    ("Sypnewsitetest", "sypnewsitetest"),
    ("Simplify Jobs Integration Sandbox", "simplifyjobsintegrationsandbox"),
    ("Remote - Referral Board", "remotereferralboardinternaluseonly"),
    ("MQ Referrals Only", "mqreferrals"),
    ("edX Bootcamps Instructional Engagement (Internal posts ONLY)", "bootcampinstructionalengagement"),
])
def test_junk_boards_are_flagged_not_relocated(name, slug):
    res = run({"company_name": name, "source": "greenhouse", "old_endpoint": slug}, {})
    assert res["status"] == "junk"


@pytest.mark.parametrize("name", ["HubSpot: Students", "Remote", "Temporal"])
def test_real_boards_are_not_junk(name):
    assert not rdb.is_junk(name, name.lower())




@pytest.mark.parametrize("company,url,trusted", [
    ("GitHub", "https://github.com/about/careers", True),
    ("dbt Labs", "https://www.getdbt.com/careers", True),
    ("Thinking Machines Lab", "https://thinkingmachines.ai/", True),
    ("Hiive", "https://www.clarity.com/careers", False),   # wrong website guess
    ("suger", "https://www.fours.com/careers/", False),
    ("mach", "https://mach.com/careers", False),           # name too short to tell
])
def test_site_matches_company(company, url, trusted):
    assert rdb.site_matches_company(company, url) is trusted


def test_guessed_wrong_website_downgrades_careers_page_hit():
    """Hiive: a guessed website (clarity.com) linked to someone else's Ashby board."""
    routes = {
        "https://api.ashbyhq.com/posting-api/job-board/claritymovescapital": FakeResp(200, {"jobs": [{"descriptionPlain": "Join Clarity"}] * 4}),
    }
    with patch.object(rdb, "_guess_domain", return_value="clarity.com"), \
         patch.object(rdb, "scan_careers", return_value=([("ashby", "claritymovescapital")], [], "https://www.clarity.com/careers")):
        res = run({"company_name": "Hiive", "source": "ashby", "old_endpoint": "hiive"}, routes, guess_domain=True)
    assert res["new_endpoint"] == "claritymovescapital"
    assert res["confidence"] == "medium"
    assert "guessed" in res["evidence"]


def test_guessed_wrong_website_unsupported_ats_is_not_high():
    with patch.object(rdb, "_guess_domain", return_value="noble-example.com"), \
         patch.object(rdb, "scan_careers", return_value=([], ["workable"], "https://www.otherco.com/careers")):
        res = run({"company_name": "Noble Thing", "source": "greenhouse", "old_endpoint": "noblething"}, {}, guess_domain=True)
    assert res["status"] == "unsupported_ats" and res["confidence"] == "medium"


def test_record_domain_is_trusted_even_if_name_differs():
    with patch.object(rdb, "scan_careers", return_value=([], ["icims"], "https://careers.example.com/jobs")):
        res = run({"company_name": "Zeta", "source": "greenhouse", "old_endpoint": "zeta", "domain": "example.com"}, {})
    assert res["status"] == "unsupported_ats" and res["confidence"] == "high"
