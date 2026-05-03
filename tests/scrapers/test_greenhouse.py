"""
Test script to verify Greenhouse API connection and job fetching.
"""
import os
import sys
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sources.api.greenhouse_source import GreenhouseSource
from utils.source_loader import get_source_config

load_dotenv()


def test_greenhouse():
    """Test Greenhouse API source."""
    print("Testing Greenhouse API source...")
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
    print(f"   Schedule: Every {config['schedule_hours']} hours")
    print()
    
    # Create source instance
    source = GreenhouseSource(
        name=config["name"],
        source_id=config["id"],
        config=config.get("config", {}),
        rate_limit_per_minute=config.get("rate_limit_per_minute", 60)
    )
    
    # Test with Airbnb
    print("Testing with Airbnb...")
    jobs = source.fetch_jobs("airbnb", "Airbnb")
    
    print(f"✅ Fetched {len(jobs)} jobs from Airbnb")
    print()
    
    if jobs:
        print("Sample job:")
        job = jobs[0]
        print(f"  Title: {job.job_title}")
        print(f"  Company: {job.company}")
        print(f"  Location: {job.location}")
        print(f"  URL: {job.url}")
        print(f"  Date Posted: {job.date_posted}")
        print()
    
    print("✅ Greenhouse API test completed successfully!")


if __name__ == "__main__":
    try:
        test_greenhouse()
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
