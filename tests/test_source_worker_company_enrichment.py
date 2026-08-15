from unittest.mock import MagicMock, patch
from uuid import UUID

from workers.source_worker import SourceWorker


COMPANY_ID = "11111111-1111-1111-1111-111111111111"


def _company(logo_url=None):
    return {
        "id": COMPANY_ID,
        "company_name": "Acme",
        "normalized_name": "acme",
        "company_endpoint": "acme",
        "logo_url": logo_url,
        "domain": None,
    }


def _source(job):
    source = MagicMock()
    source.name = "greenhouse"
    source.source_id = "source-id"
    source.fetch_jobs.return_value = [job]
    return source


@patch("workers.source_worker.update_source_last_run")
@patch("workers.source_worker.ScrapeRunRecorder.start", return_value=None)
@patch("services.company_scrape.update_company_last_fetched")
@patch("services.company_scrape.archive_jobs_missing_from_fetch", return_value=0)
@patch("services.company_scrape.seen_keys_from_jobs", return_value=(set(), set()))
@patch("services.company_scrape.save_jobs", return_value=(1, 0))
@patch("services.company_scrape.queue_existing_company_enrichment")
@patch("workers.source_worker.get_source_companies")
def test_missing_logo_queues_existing_company_enrichment(
    get_companies,
    queue_enrichment,
    _save_jobs,
    _seen,
    _archive,
    _update_company,
    _recorder_start,
    _update_source,
):
    get_companies.return_value = [_company()]
    job = MagicMock()
    job.company_domain_hint = "acme.example"

    SourceWorker(_source(job)).run()

    assert job.company_id == UUID(COMPANY_ID)
    queue_enrichment.assert_called_once_with(
        company_name="Acme",
        normalized_name="acme",
        source_name="greenhouse",
        source_endpoint="acme",
        stored_domain=None,
        source_domain_hint="acme.example",
    )


@patch("workers.source_worker.update_source_last_run")
@patch("workers.source_worker.ScrapeRunRecorder.start", return_value=None)
@patch("services.company_scrape.update_company_last_fetched")
@patch("services.company_scrape.archive_jobs_missing_from_fetch", return_value=0)
@patch("services.company_scrape.seen_keys_from_jobs", return_value=(set(), set()))
@patch("services.company_scrape.save_jobs", return_value=(1, 0))
@patch("services.company_scrape.queue_existing_company_enrichment")
@patch("workers.source_worker.get_source_companies")
def test_existing_logo_does_not_queue_enrichment(
    get_companies,
    queue_enrichment,
    _save_jobs,
    _seen,
    _archive,
    _update_company,
    _recorder_start,
    _update_source,
):
    get_companies.return_value = [_company("https://cdn.example.com/acme.png")]
    job = MagicMock()
    job.company_domain_hint = None

    SourceWorker(_source(job)).run()

    queue_enrichment.assert_not_called()
