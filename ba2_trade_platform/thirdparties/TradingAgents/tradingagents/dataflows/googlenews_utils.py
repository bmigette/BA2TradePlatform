import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import time
import random
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_result,
)


def is_rate_limited(response):
    """Check if the response indicates rate limiting (status code 429)"""
    return response.status_code == 429


@retry(
    retry=(retry_if_result(is_rate_limited)),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(5),
)
def make_request(url, headers):
    """Make a request with retry logic for rate limiting"""
    # Random delay before each request to avoid detection
    time.sleep(random.uniform(2, 6))
    response = requests.get(url, headers=headers)
    return response


def getNewsData(query, start_date, end_date):
    """
    Scrape Google News search results for a given query and date range.
    query: str - search query
    start_date: str - start date in the format yyyy-mm-dd or mm/dd/yyyy
    end_date: str - end date in the format yyyy-mm-dd or mm/dd/yyyy
    """
    if "-" in start_date:
        start_date = datetime.strptime(start_date, "%Y-%m-%d")
        start_date = start_date.strftime("%m/%d/%Y")
    if "-" in end_date:
        end_date = datetime.strptime(end_date, "%Y-%m-%d")
        end_date = end_date.strftime("%m/%d/%Y")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
        # Add cookie consent to bypass the consent page
        "Cookie": "CONSENT=YES+cb.20210720-07-p0.en+FX+410; SOCS=CAISHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo-KnBg",
    }

    news_results = []
    page = 0
    while True:
        offset = page * 10
        url = (
            f"https://www.google.com/search?q={query}"
            f"&tbs=cdr:1,cd_min:{start_date},cd_max:{end_date}"
            f"&tbm=nws&start={offset}"
        )

        try:
            response = make_request(url, headers)
            soup = BeautifulSoup(response.content, "html.parser")
            
            # Check if we got redirected to consent page  
            # Look for the base tag that indicates consent.google.com
            base_tag = soup.find('base')
            if base_tag and 'consent.google.com' in str(base_tag.get('href', '')):
                print("WARNING: Google is showing consent page. Google News scraping may be blocked.")
                print("Consider using alternative news sources (OpenAI, Alpha Vantage, etc.)")
                break
            
            # Also check the URL (in case of redirect)
            if 'consent.google.com' in response.url:
                print("WARNING: Redirected to Google consent page. Google News scraping may be blocked.")
                print("Consider using alternative news sources (OpenAI, Alpha Vantage, etc.)")
                break
            
            # Try multiple selectors (Google changes these frequently)
            selectors_to_try = [
                "div.SoaBEf",  # Original selector
                "div.Gx5Zad",  # Alternative 1
                "div.dbsr",    # Alternative 2
                "div.n0jPhd",  # Alternative 3
            ]
            
            results_on_page = []
            for selector in selectors_to_try:
                results_on_page = soup.select(selector)
                if results_on_page:
                    if page == 0:  # Only print on first page
                        print(f"Using selector: {selector} ({len(results_on_page)} results)")
                    break

            if not results_on_page:
                print("WARNING: No news elements found. Google News HTML structure may have changed.")
                break  # No more results found

            for el in results_on_page:
                try:
                    link = el.find("a")["href"]
                    
                    # Try multiple selectors for title
                    title_elem = el.select_one("div.MBeuO") or el.select_one("div.n0jPhd") or el.select_one("div.mCBkyc")
                    if not title_elem:
                        continue
                    title = title_elem.get_text()
                    
                    # Try multiple selectors for snippet
                    snippet_elem = el.select_one(".GI74Re") or el.select_one(".Y3v8qd") or el.select_one(".s3v9rd")
                    snippet = snippet_elem.get_text() if snippet_elem else ""
                    
                    # Try multiple selectors for date
                    date_elem = el.select_one(".LfVVr") or el.select_one(".OSrXXb") or el.select_one("span.r0bn4c")
                    date = date_elem.get_text() if date_elem else ""
                    
                    # Try multiple selectors for source
                    source_elem = el.select_one(".NUnG9d span") or el.select_one(".CEMjEf") or el.select_one("span.vr1PYe")
                    source = source_elem.get_text() if source_elem else ""
                    
                    news_results.append(
                        {
                            "link": link,
                            "title": title,
                            "snippet": snippet,
                            "date": date,
                            "source": source,
                        }
                    )
                except Exception as e:
                    print(f"Error processing result: {e}")
                    # If one of the fields is not found, skip this result
                    continue

            # Update the progress bar with the current count of results scraped

            # Check for the "Next" link (pagination)
            next_link = soup.find("a", id="pnnext")
            if not next_link:
                break

            page += 1

        except Exception as e:
            print(f"Failed after multiple retries: {e}")
            break

    return news_results
