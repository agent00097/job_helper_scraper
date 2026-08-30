"""Unit tests for SuccessFactorsSource — RMK tiles, URL parse, selective detail."""
from unittest.mock import MagicMock, patch

import requests

from sources.api.successfactors_source import (
    SuccessFactorsSource,
    city_from_slug,
    parse_successfactors_job_url,
    parse_tiles,
    resolve_board_base,
)

SAP_JOB_URL = (
    "https://jobs.sap.com/job/Bucharest-SAP-AI-Engineering-Architect-0030144/"
    "1381258133/"
)


def _source() -> SuccessFactorsSource:
    return SuccessFactorsSource(
        name="successfactors",
        source_id="test-id",
        config={},
        rate_limit_per_minute=600,
    )


def _tile_html(job_id: str, title: str, slug: str) -> str:
    path = f"/job/{slug}/{job_id}/"
    return f"""
    <li class="job-tile job-id-{job_id}" data-url="{path}">
      <div class="sub-section-desktop">
        <a class="jobTitle-link" href="{path}">{title}</a>
      </div>
      <div class="sub-section-tablet">
        <a class="jobTitle-link" href="{path}">{title}</a>
      </div>
      <span class="sub-section-mobile">
        <a class="jobTitle-link" href="{path}">{title}</a>
      </span>
    </li>
    """


def _detail_html(title: str, location: str, description: str) -> str:
    return f"""
    <div class="jobDisplayShell" itemscope itemtype="http://schema.org/JobPosting">
      <meta itemprop="datePosted" content="Sun Aug 30 02:00:00 UTC 2026">
      <meta itemprop="hiringOrganization" content="SAP">
      <div class="jobTitle">
        <a class="btn apply dialogApplyBtn" href="/talentcommunity/apply/1381258133/?locale=en_US">Apply now »</a>
      </div>
      <h1><span itemprop="title">{title}</span></h1>
      <span itemprop="description">
        <span class="jobdescription">
          <p><strong>Employment Type: Regular Full Time<br>Career Level: T4</strong></p>
          <p>{description}</p>
        </span>
      </span>
      <p id="job-location" class="jobLocation">
        <span class="jobGeoLocation">{location}</span>
      </p>
    </div>
    """


def test_resolve_board_base_strips_search_and_query():
    assert resolve_board_base("https://jobs.sap.com") == "https://jobs.sap.com"
    assert (
        resolve_board_base(
            "https://jobs.sap.com/search/?createNewAlert=false&q="
        )
        == "https://jobs.sap.com"
    )
    assert (
        resolve_board_base("https://jobs.sap.com/tile-search-results/?startrow=0")
        == "https://jobs.sap.com"
    )


def test_parse_job_url():
    assert parse_successfactors_job_url(SAP_JOB_URL) == (
        "https://jobs.sap.com",
        "1381258133",
    )
    assert parse_successfactors_job_url("https://jobs.sap.com/search/?q=") is None
    assert parse_successfactors_job_url("https://example.com/job/123") is None


def test_city_from_slug_bucharest():
    assert (
        city_from_slug(
            "/job/Bucharest-SAP-AI-Engineering-Architect-0030144/1381258133/",
            "SAP AI Engineering Architect",
        )
        == "Bucharest"
    )


def test_parse_tiles_dedups_responsive_copies():
    html = "<ul>" + _tile_html(
        "1381258133",
        "SAP AI Engineering Architect",
        "Bucharest-SAP-AI-Engineering-Architect-0030144",
    ) + "</ul>"
    tiles = parse_tiles(html, "https://jobs.sap.com")
    assert len(tiles) == 1
    assert tiles[0]["id"] == "1381258133"
    assert tiles[0]["title"] == "SAP AI Engineering Architect"
    assert tiles[0]["url"] == SAP_JOB_URL
    assert tiles[0]["location"] == "Bucharest"


def test_parse_job_list_only_skips_description():
    source = _source()
    job = source._parse_job(
        {
            "id": "1381258133",
            "title": "SAP AI Engineering Architect",
            "url": SAP_JOB_URL,
            "location": "Bucharest",
        },
        "SAP",
        "https://jobs.sap.com",
        detail=None,
    )
    assert job is not None
    assert job.job_description is None
    assert job.job_id_from_source == "1381258133"
    assert job.company_domain_hint == "sap.com"
    assert str(job.url) == SAP_JOB_URL


def test_parse_job_detail_builds_description_and_apply_url():
    source = _source()
    detail = source._parse_detail_html(
        _detail_html(
            "SAP AI Engineering Architect",
            "Bucharest, RO, 0030144",
            "Lead the design of AI-driven solutions.",
        ),
        SAP_JOB_URL,
    )
    assert detail is not None
    job = source._parse_job(
        {
            "id": "1381258133",
            "title": "SAP AI Engineering Architect",
            "url": SAP_JOB_URL,
            "location": "Bucharest",
        },
        "Configured",
        "https://jobs.sap.com",
        detail=detail,
    )
    assert job is not None
    assert job.company == "SAP"
    assert job.job_title == "SAP AI Engineering Architect"
    assert job.location == "Bucharest, RO, 0030144"
    assert job.employment_type == "Regular Full Time"
    assert "Lead the design" in (job.job_description or "")
    assert "talentcommunity/apply/1381258133" in str(job.application_url)
    assert job.date_posted is not None
    assert job.date_posted.year == 2026


def test_fetch_jobs_skips_detail_when_description_already_stored():
    source = _source()
    known = {SAP_JOB_URL}

    with patch.object(
        source,
        "_fetch_all_postings",
        return_value=[
            {
                "id": "1381258133",
                "title": "Keep Me",
                "url": SAP_JOB_URL,
                "location": "Bucharest",
            },
            {
                "id": "99",
                "title": "New Role",
                "url": "https://jobs.sap.com/job/Walldorf-New-Role-69190/99/",
                "location": "Walldorf",
            },
        ],
    ), patch(
        "sources.api.successfactors_source.urls_with_existing_description",
        return_value=known,
    ), patch.object(
        source,
        "_fetch_detail",
        return_value={
            "title": "New Role",
            "description": "New role description",
            "location": "Walldorf, DE, 69190",
            "date_posted": None,
            "company": "SAP",
            "employment_type": None,
            "apply_url": None,
        },
    ) as mock_detail:
        jobs = source.fetch_jobs("https://jobs.sap.com", "SAP")

    assert len(jobs) == 2
    by_id = {j.job_id_from_source: j for j in jobs}
    assert by_id["1381258133"].job_description is None
    assert "New role description" in (by_id["99"].job_description or "")
    mock_detail.assert_called_once_with(
        "https://jobs.sap.com/job/Walldorf-New-Role-69190/99/"
    )


def test_fetch_all_postings_paginates_by_tile_count():
    source = _source()
    page1 = MagicMock()
    page1.raise_for_status = MagicMock()
    page1.text = _tile_html("1", "Role A", "City-Role-A-1")
    page2 = MagicMock()
    page2.raise_for_status = MagicMock()
    page2.text = _tile_html("2", "Role B", "City-Role-B-2")
    page3 = MagicMock()
    page3.raise_for_status = MagicMock()
    page3.text = "<ul></ul>"

    with patch.object(
        source,
        "_http",
    ) as mock_http:
        mock_http.get.side_effect = [page1, page2, page3]
        postings = source._fetch_all_postings("https://jobs.sap.com", "SAP")

    assert [p["id"] for p in postings] == ["1", "2"]
    assert mock_http.get.call_count == 3
    assert mock_http.get.call_args_list[0].kwargs["params"] == {"startrow": 0}
    assert mock_http.get.call_args_list[1].kwargs["params"] == {"startrow": 1}
    assert mock_http.get.call_args_list[2].kwargs["params"] == {"startrow": 2}


def test_fetch_all_postings_raises_on_http_error():
    source = _source()
    with patch.object(
        source._http,
        "get",
        side_effect=requests.exceptions.HTTPError("404"),
    ):
        try:
            source._fetch_all_postings("https://jobs.sap.com", "SAP")
            raise AssertionError("expected HTTPError")
        except requests.exceptions.HTTPError:
            pass


def test_fetch_jobs_caps_detail_fetches_per_run():
    source = SuccessFactorsSource(
        name="successfactors",
        source_id="test-id",
        config={"max_detail_fetches_per_run": 1, "detail_workers": 1},
        rate_limit_per_minute=600,
    )
    postings = [
        {
            "id": "1",
            "title": "Role A",
            "url": "https://jobs.sap.com/job/City-Role-A-1/1/",
            "location": "City",
        },
        {
            "id": "2",
            "title": "Role B",
            "url": "https://jobs.sap.com/job/City-Role-B-2/2/",
            "location": "City",
        },
        {
            "id": "3",
            "title": "Role C",
            "url": "https://jobs.sap.com/job/City-Role-C-3/3/",
            "location": "City",
        },
    ]
    with patch.object(source, "_fetch_all_postings", return_value=postings), patch(
        "sources.api.successfactors_source.urls_with_existing_description",
        return_value=set(),
    ), patch.object(
        source,
        "_fetch_detail",
        return_value={
            "title": "Role A",
            "description": "Only first detail",
            "location": "City",
            "date_posted": None,
            "company": "SAP",
            "employment_type": None,
            "apply_url": None,
        },
    ) as mock_detail:
        jobs = source.fetch_jobs("https://jobs.sap.com", "SAP")

    assert len(jobs) == 3
    mock_detail.assert_called_once_with("https://jobs.sap.com/job/City-Role-A-1/1/")
    by_id = {j.job_id_from_source: j for j in jobs}
    assert "Only first detail" in (by_id["1"].job_description or "")
    assert by_id["2"].job_description is None
    assert by_id["3"].job_description is None


def test_fetch_job_by_url_uses_detail():
    source = _source()
    with patch.object(
        source,
        "_fetch_detail",
        return_value={
            "title": "SAP AI Engineering Architect",
            "description": "Lead AI solutions.",
            "location": "Bucharest, RO, 0030144",
            "date_posted": None,
            "company": "SAP",
            "employment_type": "Regular Full Time",
            "apply_url": "https://jobs.sap.com/talentcommunity/apply/1381258133/?locale=en_US",
        },
    ) as mock_detail:
        job = source.fetch_job_by_url(SAP_JOB_URL)

    mock_detail.assert_called_once_with(SAP_JOB_URL)
    assert job is not None
    assert job.job_title == "SAP AI Engineering Architect"
    assert job.job_id_from_source == "1381258133"
    assert "Lead AI solutions" in (job.job_description or "")
