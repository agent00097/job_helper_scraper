"""
Standalone test for AshbySource — no database required.

Run from repo root:
    python tests/test_ashby.py
    python -m tests.test_ashby
"""
import os
import sys
import requests

# Bootstrap: add repo root so imports resolve without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.api.ashby_source import AshbySource

# Linear is a well-known Ashby customer. If this stops working, try:
#   "ramp", "openai", "vercel", "retool", or "notion"
TEST_COMPANY_ENDPOINT = "linear"
TEST_COMPANY_NAME = "Linear"


def main():
    source = AshbySource(
        name="ashby",
        source_id="test-standalone",
        config={},
        rate_limit_per_minute=60,
    )

    print(f"Fetching jobs for {TEST_COMPANY_NAME} ({TEST_COMPANY_ENDPOINT}) from Ashby...")
    jobs = source.fetch_jobs(TEST_COMPANY_ENDPOINT, TEST_COMPANY_NAME)

    # Second raw fetch so we can inspect unmapped API fields for the first-3 debug block.
    raw_resp = requests.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{TEST_COMPANY_ENDPOINT}",
        timeout=30,
    )
    raw_by_id = {str(j.get("id")): j for j in raw_resp.json().get("jobs", [])}

    print(f"\nTotal jobs scraped: {len(jobs)}\n")

    if not jobs:
        print("No jobs returned — check that the company endpoint is correct.")
        return

    display = jobs[:20]
    print(f"--- First {len(display)} jobs ---\n")
    for i, job in enumerate(display, 1):
        desc_len = len(job.job_description) if job.job_description else 0
        if job.job_description and desc_len > 300:
            head = job.job_description[:150].replace("\n", " ")
            tail = job.job_description[-150:].replace("\n", " ")
            desc_preview = f"{head!r} ... {tail!r}"
        elif job.job_description:
            desc_preview = repr(job.job_description.replace("\n", " "))
        else:
            desc_preview = "None"

        print(f"[{i}] {job.job_title}")
        print(f"    Location    : {job.location}")
        print(f"    Type        : {job.employment_type}")
        print(f"    Remote      : {job.remote_allowed}")
        print(f"    Date posted : {job.date_posted}")
        print(f"    URL         : {job.url}")
        print(f"    Desc length : {desc_len} chars")
        print(f"    Description : {desc_preview}")
        if i <= 3:
            if job.job_description and desc_len > 300:
                mid = desc_len // 2
                snippet = job.job_description[mid:mid + 300].replace("\n", " ")
                print(f"    MIDDLE SECTION ({mid}–{mid + 300}): {snippet!r}")
            raw = raw_by_id.get(job.job_id_from_source, {})
            raw_is_remote = raw.get("isRemote", "<not found>")
            print(f"    Raw isRemote : {raw_is_remote!r}  →  parsed remote_allowed={job.remote_allowed}")
        print()


if __name__ == "__main__":
    main()
