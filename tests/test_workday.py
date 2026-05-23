"""
Standalone test for WorkdaySource — no database required.

Run from repo root:
    python tests/test_workday.py
    python -m tests.test_workday

Workday board URLs are tenant-specific. If the hardcoded URL returns 403 or empty
results, the tenant or data-center slug (wd1/wd3/wd5/wd12/...) may need adjustment.
Known alternatives to try:
    https://ppg.wd5.myworkdayjobs.com/PPG_Professional
    https://hp.wd5.myworkdayjobs.com/ExternalCareerSite
    https://deloitte.wd1.myworkdayjobs.com/en-US/DTUSAExternalCareers
    https://amd.wd1.myworkdayjobs.com/en-US/AMD
"""
import os
import sys
import requests as _req

# Bootstrap: add repo root so imports resolve without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.api.workday_source import WorkdaySource

TEST_COMPANY_ENDPOINT = "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"
TEST_COMPANY_NAME = "Salesforce"
MAX_JOBS = 20


class _TestWorkdaySource(WorkdaySource):
    """Single-page variant that requests MAX_JOBS in one call, skipping pagination.

    Without this cap, the real source would paginate through the entire board
    and fire a detail GET for every job — hundreds of requests during a test.
    """

    def _fetch_all_postings(self, api_base: str, company_name: str) -> list:
        self.rate_limiter.wait_if_needed()
        payload = {
            "appliedFacets": {},
            "limit": MAX_JOBS,
            "offset": 0,
            "searchText": "",
        }
        try:
            resp = _req.post(
                f"{api_base}/jobs",
                json=payload,
                timeout=30,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  List fetch failed: {e}")
            return []
        total = data.get("total", 0)
        postings = data.get("jobPostings") or []
        print(f"  Board total: {total} jobs — fetching details for first {len(postings)}")
        return postings


def main():
    source = _TestWorkdaySource(
        name="workday",
        source_id="test-standalone",
        config={},
        rate_limit_per_minute=30,  # conservative: list call + 1 detail GET per job
    )

    print(f"Fetching jobs for {TEST_COMPANY_NAME}...")
    print(f"Board URL : {TEST_COMPANY_ENDPOINT}\n")

    jobs = source.fetch_jobs(TEST_COMPANY_ENDPOINT, TEST_COMPANY_NAME)

    print(f"\nJobs returned: {len(jobs)}\n")

    if not jobs:
        print("No jobs returned — check the board URL and try the alternatives in the docstring.")
        return

    print(f"--- First {len(jobs)} jobs ---\n")
    for i, job in enumerate(jobs, 1):
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
        print(f"    Hybrid      : {job.hybrid_allowed}")
        print(f"    Date posted : {job.date_posted}")
        print(f"    URL         : {job.url}")
        print(f"    Desc length : {desc_len} chars")
        print(f"    Description : {desc_preview}")
        print()


if __name__ == "__main__":
    main()
