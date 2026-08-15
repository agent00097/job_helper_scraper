import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

from services.company_scrape_queue import process_company_scrape_body
from services.scrape_request_service import MessageDisposition


def _task(**overrides):
    body = {
        "run_id": str(uuid4()),
        "source_id": str(uuid4()),
        "source_name": "ashby",
        "company_id": str(uuid4()),
        "company_name": "Acme",
        "normalized_name": "acme",
        "company_endpoint": "acme",
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


def test_invalid_json_is_dropped():
    assert process_company_scrape_body(b"not-json") == MessageDisposition.NACK_NO_REQUEUE


def test_missing_fields_dropped():
    assert process_company_scrape_body(b"{}") == MessageDisposition.NACK_NO_REQUEUE


@patch("services.company_scrape_queue.scrape_one_company")
@patch("services.company_scrape_queue.ScrapeRunRecorder.attach")
@patch("services.company_scrape_queue._source_for")
def test_ack_on_success(source_for, attach, scrape):
    source_for.return_value = MagicMock()
    recorder = MagicMock()
    ctx = MagicMock()
    recorder.company.return_value = ctx
    ctx.__enter__.return_value = ctx
    ctx.__exit__.return_value = False
    attach.return_value = recorder
    scrape.return_value = MagicMock(
        ok=True,
        jobs_fetched=3,
        jobs_saved=1,
        jobs_duplicates=2,
        jobs_archived=0,
        error=None,
    )

    assert process_company_scrape_body(_task()) == MessageDisposition.ACK
    ctx.mark_success.assert_called_once()
    scrape.assert_called_once()
