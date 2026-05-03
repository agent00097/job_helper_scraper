"""
Test script to verify job description fetching from Greenhouse API.
"""
import os
import sys
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sources.api.greenhouse_source import GreenhouseSource
from utils.source_loader import get_source_config

load_dotenv()


def test_job_description():
    """Test Greenhouse API source with job description fetching."""
    print("Testing Greenhouse API source with job descriptions...")
    print()
    
    # Load Greenhouse config from database
    config = get_source_config("greenhouse")
    if not config:
        print("❌ Greenhouse source not found in database!")
        print("Please run the setup scripts first:")
        print("  cd schemas && ./setup_all_schemas.sh")
        return
    
    print(f"✅ Found Greenhouse source: {config['name']}")
    print(f"   Rate limit: {config['rate_limit_per_minute']} requests/minute")
    print()
    
    # Create source instance
    source = GreenhouseSource(
        name=config["name"],
        source_id=config["id"],
        config=config.get("config", {}),
        rate_limit_per_minute=config.get("rate_limit_per_minute", 60)
    )
    
    # Test with Airbnb (should fetch just a few jobs to test)
    print("Testing with Airbnb (fetching jobs with descriptions)...")
    print("This may take a moment as we fetch descriptions for each job...")
    print()
    
    jobs = source.fetch_jobs("airbnb", "Airbnb")
    
    print(f"✅ Fetched {len(jobs)} jobs from Airbnb")
    print()
    
    if jobs:
        print("Sample job with description:")
        job = jobs[0]
        print(f"  Title: {job.job_title}")
        print(f"  Company: {job.company}")
        print(f"  Location: {job.location}")
        print(f"  URL: {job.url}")
        print(f"  Date Posted: {job.date_posted}")
        print()
        
        if job.job_description:
            description_preview = job.job_description[:500] + "..." if len(job.job_description) > 500 else job.job_description
            print(f"  Description (preview):")
            print(f"  {description_preview}")
            print()
            print(f"  Full description length: {len(job.job_description)} characters")
        else:
            print("  ⚠️  No description found for this job")
        print()
    else:
        print("⚠️  No jobs found")
    
    print("✅ Job description test completed!")


if __name__ == "__main__":
    try:
        test_job_description()
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
