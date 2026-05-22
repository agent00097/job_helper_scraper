"""
Standalone test for LeverSource — no database required.

Run from repo root:
    python tests/test_lever.py
    python -m tests.test_lever
"""
import os
import sys

# Bootstrap: add repo root so imports resolve without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.api.lever_source import LeverSource

# Spotify is a well-known Lever customer (209 active jobs as of 2026-05).
# If this stops working, try: "netflix", "figma", "brex", "mercury", "rippling"
TEST_COMPANY_ENDPOINT = "spotify"
TEST_COMPANY_NAME = "Spotify"


def main():
    source = LeverSource(
        name="lever",
        source_id="test-standalone",
        config={},
        rate_limit_per_minute=60,
    )

    print(f"Fetching jobs for {TEST_COMPANY_NAME} ({TEST_COMPANY_ENDPOINT}) from Lever...")
    jobs = source.fetch_jobs(TEST_COMPANY_ENDPOINT, TEST_COMPANY_NAME)

    print(f"\nTotal jobs scraped: {len(jobs)}\n")

    if not jobs:
        print("No jobs returned — check that the company endpoint is correct.")
        return

    display = jobs[:20]
    print(f"--- First {len(display)} jobs ---\n")
    for i, job in enumerate(display, 1):
        desc_len = len(job.job_description) if job.job_description else 0
        desc_preview = (
            repr(job.job_description[:150].replace("\n", " "))
            if job.job_description
            else "None"
        )

        print(f"[{i}] {job.job_title}")
        print(f"    Location    : {job.location}")
        print(f"    Type        : {job.employment_type}")
        print(f"    Remote      : {job.remote_allowed}")
        print(f"    Hybrid      : {job.hybrid_allowed}")
        print(f"    Date posted : {job.date_posted}")
        print(f"    URL         : {job.url}")
        print(f"    Desc length : {desc_len} chars")
        print(f"    Description : {desc_preview}")
        if i <= 2 and job.job_description:
            print(f"    --- FULL DESCRIPTION (job {i}) ---")
            print(job.job_description)
            print(f"    --- END FULL DESCRIPTION (job {i}) ---")
        print()


if __name__ == "__main__":
    main()
