"""Worker recycle / HTML parse-tree cleanup."""
from unittest.mock import MagicMock, patch

from utils.html_text import html_to_text
from utils.process_hygiene import (
    note_company_task_finished,
    recycle_requested,
    request_recycle,
    reset_recycle_state_for_tests,
)


def test_html_to_text_strips_tags():
    assert "Hello" in html_to_text("<p>Hello&nbsp;world</p>")
    assert html_to_text("") == ""


@patch.dict("os.environ", {"COMPANY_SCRAPE_MAX_TASKS_PER_PROCESS": "2"}, clear=False)
def test_max_tasks_per_process_requests_recycle():
    reset_recycle_state_for_tests()
    note_company_task_finished()
    assert recycle_requested() is False
    note_company_task_finished()
    assert recycle_requested() is True
    reset_recycle_state_for_tests()


def test_successfactors_release_resources_replaces_session():
    from sources.api.successfactors_source import SuccessFactorsSource

    source = SuccessFactorsSource(
        name="successfactors",
        source_id="test-id",
        config={},
        rate_limit_per_minute=60,
    )
    first = source._http
    source.release_resources()
    assert source._http is not first


def test_stop_consumer_for_recycle_after_ack():
    from utils.process_hygiene import stop_consumer_for_recycle

    reset_recycle_state_for_tests()
    channel = MagicMock()
    assert stop_consumer_for_recycle(channel) is False
    channel.stop_consuming.assert_not_called()

    request_recycle("test")
    assert stop_consumer_for_recycle(channel, already_stopping=True) is False
    assert stop_consumer_for_recycle(channel) is True
    channel.stop_consuming.assert_called_once()
    reset_recycle_state_for_tests()
