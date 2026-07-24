"""Unit tests for job_skills persistence helpers."""
from __future__ import annotations

from uuid import uuid4
from unittest.mock import MagicMock, patch

from utils.skills.extract import SkillHit
from utils.skills.persist import replace_job_skills


def test_replace_job_skills_writes_rows():
    job_id = uuid4()
    skill_id = uuid4()
    hits = [
        SkillHit(
            skill_id=skill_id,
            skill_name="Python",
            weight=0.9,
            method="alias",
            matched_alias="python",
        )
    ]

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    with patch("utils.skills.persist.db.get_db_connection", return_value=mock_conn):
        n = replace_job_skills(job_id, hits)

    assert n == 1
    assert mock_cur.execute.called
    assert mock_cur.executemany.called
    mock_conn.commit.assert_called_once()


def test_extract_skills_safe_swallows_errors():
    from utils.job_storage import _extract_skills_safe
    import services.skill_extraction_service as ses

    # Import happens inside _extract_skills_safe; patch the module attr used after import.
    with patch.object(ses, "extract_skills_for_job", side_effect=RuntimeError("boom")):
        _extract_skills_safe(uuid4(), "Title", "Description with Python")
