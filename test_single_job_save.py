"""
Manual test script to debug job description saving.
"""
import os
import sys
import logging
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set up detailed logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from sources.api.greenhouse_source import GreenhouseSource
from utils.source_loader import get_source_config
from utils.job_storage import save_job
import db
import requests

load_dotenv()


def test_single_job():
    """Test saving a single job with description."""
    print("=" * 60)
    print("Testing single job save with description")
    print("=" * 60)
    print()
    
    # Load config
    config = get_source_config("greenhouse")
    if not config:
        print("❌ Greenhouse source not found!")
        return
    
    # Create source
    source = GreenhouseSource(
        name=config["name"],
        source_id=config["id"],
        config=config.get("config", {}),
        rate_limit_per_minute=config.get("rate_limit_per_minute", 60)
    )
    
    # Fetch just 1 job - we'll manually fetch the list and take the first one
    print("Step 1: Fetching job list from Airbnb (just the list, not descriptions yet)...")
    api_url = f"{source.base_url}/boards/airbnb/jobs"
    response = requests.get(api_url, timeout=30)
    response.raise_for_status()
    jobs_data = response.json()
    
    if not jobs_data.get("jobs"):
        print("❌ No jobs found!")
        return
    
    # Take the first job from the list
    first_job_data = jobs_data["jobs"][0]
    job_id = first_job_data.get("id")
    
    print(f"   Found job ID: {job_id}")
    print(f"   Title: {first_job_data.get('title')}")
    print()
    
    # Parse the job
    print("Step 2: Parsing job and fetching description...")
    job = source._parse_job(first_job_data, "Airbnb", "airbnb")
    
    # Fetch description
    job_description = source._fetch_job_description("airbnb", job_id)
    if job_description:
        job.job_description = job_description
        print(f"   ✅ Description fetched: {len(job_description)} characters")
    else:
        print("   ❌ No description fetched")
        return
    print()
    print(f"✅ Parsed job: {job.job_title}")
    print(f"   URL: {job.url}")
    print(f"   Job ID: {job.job_id_from_source}")
    print(f"   Description length: {len(job.job_description) if job.job_description else 0}")
    print()
    
    if not job.job_description:
        print("❌ ERROR: Job has no description after fetching!")
        return
    
    print(f"Description preview (first 200 chars):")
    print(f"{job.job_description[:200]}...")
    print()
    
    # Check if job exists in database
    print("Step 3: Checking if job exists in database...")
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, url, job_title, job_description FROM jobs WHERE url = %s", (str(job.url),))
            existing = cur.fetchone()
            
            if existing:
                existing_id, existing_url, existing_title, existing_desc = existing
                print(f"✅ Job exists in database:")
                print(f"   ID: {existing_id}")
                print(f"   Title: {existing_title}")
                print(f"   Current description is None: {existing_desc is None}")
                print(f"   Current description length: {len(existing_desc) if existing_desc else 0}")
                if existing_desc:
                    print(f"   Current description preview: {existing_desc[:100]}...")
            else:
                print("ℹ️  Job does not exist in database (will be inserted)")
    finally:
        conn.close()
    print()
    
    # Try to save
    print("Step 4: Attempting to save/update job...")
    saved = save_job(job)
    print(f"Save result: {saved}")
    print()
    
    # Check database again
    print("Step 5: Checking database after save...")
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, url, job_title, job_description FROM jobs WHERE url = %s", (str(job.url),))
            result = cur.fetchone()
            
            if result:
                db_id, db_url, db_title, db_desc = result
                print(f"✅ Job in database:")
                print(f"   ID: {db_id}")
                print(f"   Title: {db_title}")
                print(f"   Description is None: {db_desc is None}")
                print(f"   Description length: {len(db_desc) if db_desc else 0}")
                if db_desc:
                    print(f"   Description preview: {db_desc[:200]}...")
                else:
                    print("   ❌ DESCRIPTION IS STILL NULL!")
            else:
                print("❌ Job not found in database after save!")
    finally:
        conn.close()
    print()
    
    print("=" * 60)
    print("Test completed!")
    print("=" * 60)


if __name__ == "__main__":
    try:
        test_single_job()
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
