"""
Test script to verify job descriptions are being fetched and saved correctly.
"""
import os
import sys
import logging
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set up logging to see debug messages
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from sources.api.greenhouse_source import GreenhouseSource
from utils.source_loader import get_source_config
from utils.job_storage import save_job

load_dotenv()


def test_description_save():
    """Test that job descriptions are fetched and saved."""
    print("Testing job description fetching and saving...")
    print()
    
    # Load Greenhouse config
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
    
    # Fetch just 1 job from Airbnb to test
    print("Fetching 1 job from Airbnb (this will take a moment)...")
    print()
    
    jobs = source.fetch_jobs("airbnb", "Airbnb")
    
    if not jobs:
        print("❌ No jobs fetched!")
        return
    
    # Take the first job
    job = jobs[0]
    
    print(f"✅ Fetched job: {job.job_title}")
    print(f"   URL: {job.url}")
    print(f"   Description length: {len(job.job_description) if job.job_description else 0} characters")
    print()
    
    if job.job_description:
        print(f"   Description preview (first 300 chars):")
        print(f"   {job.job_description[:300]}...")
        print()
        
        # Try to save it
        print("Attempting to save job to database...")
        saved = save_job(job)
        
        if saved:
            print("✅ Job saved successfully with description!")
        else:
            print("⚠️  Job not saved (might be duplicate)")
    else:
        print("❌ No description found for this job!")
        print("   This indicates the description fetching is not working.")
    
    print()
    print("Test completed!")


if __name__ == "__main__":
    try:
        test_description_save()
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
