# Job Description Implementation

## Overview

The Greenhouse scraper has been updated to fetch full job descriptions for each job listing. Previously, only basic job information was fetched from the list endpoint, which doesn't include descriptions.

## Changes Made

### 1. Database Schema
✅ **No changes needed** - The `jobs` table already has a `job_description TEXT` field.

### 2. Greenhouse Scraper Updates

#### Added Methods:
- **`_fetch_job_description()`**: Fetches individual job details from Greenhouse detail endpoint
  - Endpoint: `/boards/{company}/jobs/{job_id}`
  - Extracts the `content` field which contains the full job description
  - Handles errors gracefully (continues if description fetch fails)

- **`_clean_html()`**: Cleans HTML content to extract readable text
  - Decodes HTML entities (e.g., `&lt;` → `<`)
  - Removes script and style tags
  - Converts HTML tags to newlines for better readability
  - Removes all HTML tags
  - Cleans up whitespace

#### Updated Methods:
- **`fetch_jobs()`**: Now fetches descriptions for each job after getting the list
  - For each job in the list, makes an additional API call to get full description
  - Respects rate limiting for both list and detail requests
  - Continues processing even if description fetch fails for individual jobs

### 3. Rate Limiting

The rate limiter is applied to:
1. Initial list request (`/boards/{company}/jobs`)
2. Each detail request (`/boards/{company}/jobs/{job_id}`)

This means if you have 10 jobs, you'll make:
- 1 list request
- 10 detail requests
- Total: 11 requests per company

**Current rate limit**: 60 requests/minute (configurable in `job_sources` table)

### 4. Error Handling

- If description fetch fails for a job, the job is still saved (without description)
- Errors are logged as warnings, not failures
- The scraper continues processing other jobs

## How It Works

1. **Fetch Job List**: Get list of jobs from `/boards/{company}/jobs`
2. **For Each Job**:
   - Parse basic info (title, location, etc.)
   - Fetch full description from `/boards/{company}/jobs/{job_id}`
   - Clean HTML content
   - Store in database

## Testing

Run the test script to verify:

```bash
python test_job_description.py
```

This will:
- Fetch jobs from Airbnb
- Show a sample job with its description
- Display description length

## Performance Considerations

### API Calls
- **Before**: 1 request per company (list only)
- **After**: 1 + N requests per company (list + N detail requests)

### Rate Limiting Impact
With 60 requests/minute:
- Can fetch ~5 companies with 10 jobs each per minute
- Or ~1 company with 50 jobs per minute

### Recommendations
1. **Adjust rate limits** if needed in `job_sources.rate_limit_per_minute`
2. **Monitor API usage** to avoid hitting limits
3. **Consider batching** if you have many companies with many jobs

## Example Output

Job descriptions are stored as cleaned text:

```
Airbnb was born in 2007 when two hosts welcomed three guests to their San Francisco home...

The Community You Will Join:

The Airbnb Hotels team, which includes HotelTonight, is a fun, fast-growing group...

The Difference You Will Make

As an Account Executive, you will be responsible for...
```

## Future Improvements

Possible enhancements:
1. Use BeautifulSoup for more advanced HTML cleaning
2. Store both raw HTML and cleaned text
3. Extract structured data (requirements, benefits, etc.)
4. Add caching to avoid re-fetching descriptions
