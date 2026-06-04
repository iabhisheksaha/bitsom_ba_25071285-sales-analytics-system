"""
Agent 1: Job Discovery and Scraping
Searches LinkedIn, Indeed, and Naukri.com for VP-level Product Owner /
Business Analyst roles in Pune and returns structured job postings.
"""

import os
import random
import re
import time
import urllib3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from typing import Optional
from urllib.parse import quote_plus, urlencode

import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")


@dataclass
class JobPosting:
    platform: str
    title: str
    company: str
    location: str
    url: str
    jd_text: str = ""
    posted_date: str = ""
    job_id: str = ""
    tags: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Base scraper
# ---------------------------------------------------------------------------

class BaseScraper:
    def __init__(self, config: dict):
        self.config = config
        self.request_delay = config.get("scraping", {}).get("request_delay", 3)
        self.max_pages = config.get("scraping", {}).get("max_pages_per_platform", 5)
        self.user_agents: list = config.get("scraping", {}).get("user_agents", [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ])
        self.session = requests.Session()
        if SCRAPER_API_KEY:
            _proxy = f"http://scraperapi:{SCRAPER_API_KEY}@proxy-server.scraperapi.com:8001"
            self.session.proxies.update({"http": _proxy, "https": _proxy})
            self.session.verify = False
            print(f"  [Agent1] ScraperAPI proxy active (residential IP routing)")

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(self.user_agents),
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def _get(self, url: str, params: dict = None) -> Optional[BeautifulSoup]:
        try:
            time.sleep(self.request_delay + random.uniform(0, 1.5))
            resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            print(f"  [Agent1] Request failed for {url}: {exc}")
            return None

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Naukri scraper  (uses Naukri's JSON search API — no JS rendering needed)
# ---------------------------------------------------------------------------

class NaukriScraper(BaseScraper):
    # Naukri's internal JSON API — returns structured job data directly
    SEARCH_API  = "https://www.naukri.com/jobapi/v3/search"
    BASE_URL    = "https://www.naukri.com"

    def _api_headers(self) -> dict:
        return {
            **self._headers(),
            "appid":    "109",
            "systemid": "109",
            "Accept":   "application/json",
        }

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for role in roles:
            keyword = f"{role} {seniority_keywords[0]}"
            for page in range(1, self.max_pages + 1):
                params = {
                    "noOfResults": 20,
                    "urlType":     "search_by_key_loc",
                    "searchType":  "adv",
                    "keyword":     keyword,
                    "location":    location.lower(),
                    "pageNo":      page,
                }
                print(f"  [Agent1/Naukri] API page {page} for '{keyword}'")
                jobs = self._fetch_page(params)
                if not jobs:
                    break
                postings.extend(jobs)
                time.sleep(self.request_delay)
        return postings

    def _fetch_page(self, params: dict) -> list[JobPosting]:
        try:
            resp = self.session.get(
                self.SEARCH_API,
                headers=self._api_headers(),
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"  [Agent1/Naukri] API error: {exc}")
            return []

        results = []
        for j in data.get("jobDetails", []):
            title = j.get("title", "").strip()
            if not title:
                continue
            jd_url = j.get("jdURL", "")
            if jd_url and not jd_url.startswith("http"):
                jd_url = self.BASE_URL + jd_url
            jd_text = BeautifulSoup(j.get("jobDescription", ""), "html.parser").get_text("\n")
            locations = j.get("placeholders", [])
            loc_str = ", ".join(
                p.get("label", "") for p in locations if p.get("type") == "location"
            ) or "Pune"
            results.append(JobPosting(
                platform="naukri",
                title=title,
                company=j.get("companyName", "Unknown").strip(),
                location=loc_str,
                url=jd_url,
                jd_text=jd_text,
                job_id=str(j.get("jobId", "")),
            ))
        return results


# ---------------------------------------------------------------------------
# Indeed scraper  (uses Indeed RSS feed — not blocked like the HTML endpoint)
# ---------------------------------------------------------------------------

class IndeedScraper(BaseScraper):
    RSS_URL  = "https://in.indeed.com/rss"
    BASE_URL = "https://in.indeed.com"

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for role in roles:
            query = f"{role} {seniority_keywords[0]}"
            print(f"  [Agent1/Indeed] RSS feed for '{query}' in {location}")
            params = {"q": query, "l": location, "fromage": 30, "sort": "date"}
            jobs = self._fetch_rss(params)
            print(f"  [Agent1/Indeed]   → {len(jobs)} items")
            postings.extend(jobs)
            time.sleep(self.request_delay)
        return postings

    def _fetch_rss(self, params: dict) -> list[JobPosting]:
        try:
            resp = self.session.get(
                self.RSS_URL, headers=self._headers(), params=params, timeout=20
            )
            resp.raise_for_status()
        except Exception as exc:
            print(f"  [Agent1/Indeed] RSS error: {exc}")
            return []

        results = []
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            print(f"  [Agent1/Indeed] RSS parse error: {exc}")
            return []

        ns = ""  # Indeed RSS uses no namespace
        for item in root.iter("item"):
            title   = (item.findtext("title") or "").strip()
            link    = (item.findtext("link")  or "").strip()
            desc    = unescape(item.findtext("description") or "")
            company = (item.findtext("source") or "Unknown").strip()
            pub     = (item.findtext("pubDate") or "").strip()

            if not title or not link:
                continue

            # Strip HTML tags from description
            jd_text = re.sub(r"<[^>]+>", " ", desc).strip()

            results.append(JobPosting(
                platform="indeed",
                title=title,
                company=company,
                location=params.get("l", ""),
                url=link,
                jd_text=jd_text,
                posted_date=pub,
            ))
        return results


# ---------------------------------------------------------------------------
# LinkedIn scraper  (requires authenticated session)
# ---------------------------------------------------------------------------

class LinkedInScraper(BaseScraper):
    """
    LinkedIn's guest API endpoint for job search.
    Full JD text requires authentication (session cookies li_at / JSESSIONID).
    Provide cookies via config['linkedin_cookies'] if available.
    """

    SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        cookies = self.config.get("linkedin_cookies", {})
        if not cookies:
            print(
                "  [Agent1/LinkedIn] No session cookies configured. "
                "Attempting unauthenticated guest search (limited results)."
            )

        postings: list[JobPosting] = []
        for role in roles:
            for page_num in range(self.max_pages):
                params = {
                    "keywords": f"{role} VP",
                    "location": location,
                    "f_E": "4,5",   # experience: director / VP levels
                    "start": page_num * 25,
                    "sortBy": "DD",
                }
                print(f"  [Agent1/LinkedIn] Scraping page {page_num + 1} for '{role}'")
                try:
                    time.sleep(self.request_delay + random.uniform(0, 2))
                    resp = self.session.get(
                        self.SEARCH_URL,
                        headers=self._headers(),
                        params=params,
                        cookies=cookies,
                        timeout=15,
                    )
                    if resp.status_code == 429:
                        print("  [Agent1/LinkedIn] Rate limited — backing off 60s")
                        time.sleep(60)
                        continue
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, "html.parser")
                    jobs = self._parse_listing(soup)
                    if not jobs:
                        break
                    postings.extend(jobs)
                except requests.RequestException as exc:
                    print(f"  [Agent1/LinkedIn] Request error: {exc}")
                    break
        return postings

    def _parse_listing(self, soup: BeautifulSoup) -> list[JobPosting]:
        results = []
        cards = soup.select("li") or []
        for card in cards:
            title_el = card.select_one(".base-search-card__title")
            company_el = card.select_one(".base-search-card__subtitle")
            location_el = card.select_one(".job-search-card__location")
            link_el = card.select_one("a.base-card__full-link")

            if not title_el:
                continue

            href = link_el.get("href", "") if link_el else ""
            posting = JobPosting(
                platform="linkedin",
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else "Pune",
                url=href,
            )
            if posting.url:
                posting.jd_text = self._fetch_jd(posting.url)
            results.append(posting)
        return results

    def _fetch_jd(self, url: str) -> str:
        soup = self._get(url)
        if not soup:
            return ""
        jd_el = soup.select_one(".show-more-less-html__markup") or soup.select_one("[class*='description']")
        return jd_el.get_text(separator="\n", strip=True) if jd_el else ""


# ---------------------------------------------------------------------------
# Orchestrator for Agent 1
# ---------------------------------------------------------------------------

class JobDiscoveryAgent:
    """
    Coordinates all platform scrapers and returns a de-duplicated list
    of JobPosting objects.
    """

    def __init__(self, config: dict):
        self.config = config
        platforms = config.get("search", {}).get("platforms", {})
        self._scrapers: list[BaseScraper] = []
        if platforms.get("naukri", True):
            self._scrapers.append(NaukriScraper(config))
        if platforms.get("indeed", True):
            self._scrapers.append(IndeedScraper(config))
        if platforms.get("linkedin", True):
            self._scrapers.append(LinkedInScraper(config))

    def discover(self) -> list[JobPosting]:
        search = self.config.get("search", {})
        roles: list = search.get("roles", ["Product Owner", "Business Analyst"])
        seniority: list = search.get("seniority", ["VP"])
        location: str = search.get("location", "Pune")

        print(f"[Agent1] Starting job discovery — roles: {roles}, seniority: {seniority}, location: {location}")

        all_postings: list[JobPosting] = []
        for scraper in self._scrapers:
            try:
                jobs = scraper.scrape(roles, seniority, location)
                print(f"  [Agent1] {scraper.__class__.__name__} found {len(jobs)} postings")
                all_postings.extend(jobs)
            except Exception as exc:
                print(f"  [Agent1] {scraper.__class__.__name__} error: {exc}")

        unique = self._deduplicate(all_postings)
        print(f"[Agent1] Discovery complete — {len(unique)} unique postings")
        return unique

    @staticmethod
    def _deduplicate(postings: list[JobPosting]) -> list[JobPosting]:
        seen: set[str] = set()
        unique: list[JobPosting] = []
        for p in postings:
            key = (p.company.lower().strip(), p.title.lower().strip())
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique
