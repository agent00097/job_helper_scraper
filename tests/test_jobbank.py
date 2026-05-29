"""
Standalone test for JobBankSource — no database required.

Run from repo root:
    python tests/test_jobbank.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.scraper.jobbank_source import JobBankSource

logging.basicConfig(level=logging.INFO)

MAX_PAGES         = 2   # ~50 jobs (25 per page)
DESCRIPTION_COUNT = 3


def main():
    source = JobBankSource(
        name="jobbank",
        source_id="test-standalone",
        config={"max_pages": MAX_PAGES, "fetch_descriptions": False},
        rate_limit_per_minute=20,
        skip_db=True,
    )

    print(f"Fetching Job Bank (max_pages={MAX_PAGES})…")
    jobs = source.fetch_jobs("", "Job Bank Canada")

    print(f"\nTotal scraped: {len(jobs)}\n")
    print("=" * 72)
    print("First 20 jobs")
    print("=" * 72)
    for i, job in enumerate(jobs[:20]):
        if job.remote_allowed:
            work_mode = "Remote"
        elif job.hybrid_allowed:
            work_mode = "Hybrid"
        else:
            work_mode = "On site"

        print(
            f"{i+1:2}. {job.job_title or '(no title)'}\n"
            f"     Company   : {job.company or '—'}\n"
            f"     Location  : {job.location or '—'}  [country_code={job.country_code}]\n"
            f"     Work mode : {work_mode}\n"
            f"     Salary    : {job.salary_range or '—'}\n"
            f"     URL       : {job.url}\n"
            f"     Job #     : {job.job_id_from_source or '—'}\n"
        )

    print("=" * 72)
    print(f"Fetching descriptions for first {DESCRIPTION_COUNT} jobs…")
    print("=" * 72)
    for i, job in enumerate(jobs[:DESCRIPTION_COUNT]):
        desc = source.fetch_description(str(job.url))
        if desc:
            job.job_description = desc
        print(f"\n[Job {i+1}] {job.job_title or '(no title)'} @ {job.company or '—'}")
        print(f"URL: {job.url}")
        if job.job_description:
            print(f"Description ({len(job.job_description)} chars) — first 300 chars:")
            print(job.job_description[:300])
        else:
            print("Description: (none fetched)")


if __name__ == "__main__":
    main()
