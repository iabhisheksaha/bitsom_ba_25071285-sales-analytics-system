"""
Agent 1: Job Discovery and Scraping
Searches LinkedIn, Indeed, and Naukri.com for VP-level Product Owner /
Business Analyst roles in Pune and returns structured job postings.

Architecture — zero-cost primary sources
-----------------------------------------
Naukri   : Internal JSON API (naukri.com/jobapi/v3/search) — same endpoint
           the website calls; returns machine-readable job data directly.
Indeed   : Official RSS feed (in.indeed.com/rss) — documented, no auth.
LinkedIn : Guest jobs API (linkedin.com/jobs-guest/…) — no login needed.

ScraperAPI is used only as a fallback when a free approach fails AND
SCRAPER_API_KEY is set in the environment. Removing the key disables it
entirely with no impact on the primary scraping paths.
"""

import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlencode

import requests
from bs4 import BeautifulSoup

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SCRAPER_API_ENDPOINT = "https://api.scraperapi.com/"

_RENDER_TIMEOUT = 75
_PLAIN_TIMEOUT  = 45

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Headers expected by Naukri's internal JSON API
_NAUKRI_API_HEADERS = {
    "system-id":     "109",
    "Appid":         "109",
    "clientId":      "d3skt0p",
    "gid":           "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
    "User-Agent":    _DEFAULT_UA,
    "Accept-Language": "en-IN,en;q=0.9",
}


@dataclass
class JobPosting:
    platform:    str
    title:       str
    company:     str
    location:    str
    url:         str
    jd_text:     str  = ""
    posted_date: str  = ""
    job_id:      str  = ""
    tags:        list = field(default_factory=list)
    apply_url:   str  = ""   # pre-fetched external ATS URL (avoids Agent3 re-fetch)


# ---------------------------------------------------------------------------
# ScraperAPI — kept as optional fallback only (costs credits)
# ---------------------------------------------------------------------------

def scraperapi_fetch(
    target_url: str,
    render: bool = True,
    country: str = "in",
    timeout: Optional[int] = None,
) -> Optional[requests.Response]:
    """
    Fetch target_url through ScraperAPI (optional, credit-consuming).
    Only called when SCRAPER_API_KEY is set AND the free approach has failed.
    """
    if timeout is None:
        timeout = _RENDER_TIMEOUT if render else _PLAIN_TIMEOUT

    if not SCRAPER_API_KEY:
        try:
            return requests.get(
                target_url,
                headers={"User-Agent": _DEFAULT_UA, "Accept-Language": "en-IN,en;q=0.9"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            print(f"  [Agent1] Direct fetch failed for {target_url}: {exc}")
            return None

    params: dict = {
        "api_key":      SCRAPER_API_KEY,
        "url":          target_url,
        "country_code": country,
        "device_type":  "desktop",
    }
    if render:
        params["render"] = "true"

    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(SCRAPER_API_ENDPOINT, params=params, timeout=timeout)
            if resp.status_code < 500:
                if resp.status_code != 200:
                    body = (resp.text or "")[:300]
                    print(f"  [Agent1] ScraperAPI HTTP {resp.status_code} for {target_url}\n"
                          f"           body: {body}")
                return resp
            wait = 10 * (attempt + 1)
            print(f"  [Agent1] ScraperAPI HTTP {resp.status_code} (attempt {attempt+1}/3) — "
                  f"retrying in {wait}s…")
            time.sleep(wait)
        except requests.RequestException as exc:
            last_exc = exc
            wait = 10 * (attempt + 1)
            print(f"  [Agent1] ScraperAPI error (attempt {attempt+1}/3): {exc} — "
                  f"retrying in {wait}s…")
            time.sleep(wait)
    print(f"  [Agent1] ScraperAPI retries exhausted for {target_url}"
          + (f": {last_exc}" if last_exc else ""))
    return None


def _debug_dump_html(platform: str, html: str, page_no: int = 1) -> None:
    debug_dir = Path("logs/debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        out = debug_dir / f"{platform}_page{page_no}.html"
        out.write_text(html or "", encoding="utf-8")
        print(f"  [Agent1/{platform.title()}] DEBUG saved {out} ({len(html or '')} chars)")
        print(f"  [Agent1/{platform.title()}] snippet: {(html or '')[:400]}")
    except Exception as exc:
        print(f"  [Agent1/{platform.title()}] DEBUG dump failed: {exc}")


# ---------------------------------------------------------------------------
# Base scraper
# ---------------------------------------------------------------------------

class BaseScraper:
    platform = "base"

    def __init__(self, config: dict):
        self.config = config
        scraping = config.get("scraping", {})
        self.delay     = scraping.get("request_delay", 3)
        self.max_pages = min(scraping.get("max_pages_per_platform", 5), 2)

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Naukri — internal JSON API (free, no ScraperAPI)
# ---------------------------------------------------------------------------

class NaukriScraper(BaseScraper):
    platform = "naukri"
    API_SEARCH = "https://www.naukri.com/jobapi/v3/search"
    API_JOB    = "https://www.naukri.com/jobapi/v3/job"
    BASE_URL   = "https://www.naukri.com"

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for role in roles:
            keyword = f"{role} {seniority_keywords[0]}"
            for page_no in range(1, self.max_pages + 1):
                print(f"  [Agent1/Naukri] API search page {page_no}: '{keyword}' in {location}")
                jobs = self._api_search(keyword, location, page_no)
                if jobs:
                    print(f"  [Agent1/Naukri]   → {len(jobs)} jobs (JSON API)")
                    postings.extend(jobs)
                    time.sleep(self.delay)
                    continue

                # API failed — fall back to ScraperAPI render if key is set
                if SCRAPER_API_KEY:
                    print(f"  [Agent1/Naukri] API failed — trying ScraperAPI render fallback")
                    jobs = self._scraperapi_fallback(keyword, location, page_no)
                    if jobs:
                        print(f"  [Agent1/Naukri]   → {len(jobs)} jobs (ScraperAPI fallback)")
                        postings.extend(jobs)
                else:
                    print(f"  [Agent1/Naukri] API failed, no SCRAPER_API_KEY — skipping page.")
                time.sleep(self.delay)
        return postings

    def _api_search(self, keyword: str, location: str, page: int) -> list[JobPosting]:
        """Hit Naukri's internal JSON search API — same endpoint their SPA calls."""
        params = {
            "noOfResults":  20,
            "urlType":      "search_by_key_loc",
            "searchType":   "adv",
            "keyword":      keyword,
            "location":     location.lower(),
            "pageNo":       page,
            "k":            keyword,
            "l":            location.lower(),
        }
        try:
            resp = requests.get(
                self.API_SEARCH,
                params=params,
                headers=_NAUKRI_API_HEADERS,
                timeout=20,
            )
            if not resp.ok:
                print(f"  [Agent1/Naukri] API HTTP {resp.status_code}")
                return []
            data = resp.json()
            job_list = data.get("jobDetails") or data.get("jobs") or []
            if not job_list:
                # Try nested structure
                job_list = _deep_find_jobdetails(data)
            return [self._parse_job_obj(j) for j in job_list if j.get("title")]
        except Exception as exc:
            print(f"  [Agent1/Naukri] API error: {exc}")
            return []

    def get_apply_url(self, job_id: str) -> str:
        """Fetch external apply URL for a job via Naukri's job detail API (free)."""
        if not job_id:
            return ""
        try:
            resp = requests.get(
                self.API_JOB,
                params={"jobId": job_id},
                headers=_NAUKRI_API_HEADERS,
                timeout=20,
            )
            if not resp.ok:
                return ""
            data = resp.json()
            return _naukri_extract_apply_url(data) or ""
        except Exception:
            return ""

    def _scraperapi_fallback(self, keyword: str, location: str, page: int) -> list[JobPosting]:
        slug = re.sub(r"\s+", "-", keyword.lower())
        loc  = location.lower()
        url  = f"{self.BASE_URL}/{slug}-jobs-in-{loc}-{page}"
        resp = scraperapi_fetch(url, render=True, country="in")
        if resp is None or resp.status_code != 200:
            return []
        return self._extract_html(resp.text)

    def _parse_job_obj(self, j: dict) -> JobPosting:
        jd_url = j.get("jdURL") or j.get("jobUrl") or ""
        if jd_url and not jd_url.startswith("http"):
            jd_url = self.BASE_URL + jd_url

        placeholders = j.get("placeholders") or j.get("locations") or []
        loc = "Pune"
        for ph in placeholders:
            if isinstance(ph, dict) and ph.get("type") == "location":
                loc = ph.get("label", "Pune")
                break
            elif isinstance(ph, str):
                loc = ph
                break

        return JobPosting(
            platform="naukri",
            title=j.get("title", "").strip(),
            company=j.get("companyName", "Unknown").strip(),
            location=loc,
            url=jd_url,
            jd_text=BeautifulSoup(j.get("jobDescription", ""), "html.parser").get_text("\n"),
            job_id=str(j.get("jobId", "")),
            apply_url=_naukri_extract_apply_url(j),
        )

    def _extract_html(self, html: str) -> list[JobPosting]:
        """HTML fallback parser (used only if API + ScraperAPI both needed)."""
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            try:
                data = json.loads(script.string)
                job_list = _deep_find_jobdetails(data)
                if job_list:
                    return [self._parse_job_obj(j) for j in job_list if j.get("title")]
            except Exception as exc:
                print(f"  [Agent1/Naukri] HTML __NEXT_DATA__ parse error: {exc}")
        return self._parse_html_cards(soup)

    def _parse_html_cards(self, soup: BeautifulSoup) -> list[JobPosting]:
        cards = (
            soup.select("article.srp-jobtuple-wrapper") or
            soup.select("[class*='srp-jobtuple']") or
            soup.select("article.jobTuple") or
            soup.select("[data-job-id]")
        )
        results = []
        for card in cards:
            title_el = (
                card.select_one("a.title") or
                card.select_one("[class*='title'] a") or
                card.select_one("h2 a")
            )
            if not title_el:
                continue
            company_el = card.select_one("a.comp-name") or card.select_one("[class*='comp-name']")
            loc_el = card.select_one("span.locWdth") or card.select_one("[class*='location']")
            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = self.BASE_URL + href
            results.append(JobPosting(
                platform="naukri",
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=loc_el.get_text(strip=True) if loc_el else "Pune",
                url=href,
                job_id=card.get("data-job-id", ""),
            ))
        return results


def _naukri_extract_apply_url(data: dict) -> str:
    """
    Extract an external ATS apply URL from a Naukri job object.
    Checks both specific known keys and any string value matching an ATS domain.
    """
    _ATS_DOMAINS = (
        "workday.com", "myworkdayjobs.com", "greenhouse.io", "lever.co",
        "icims.com", "taleo.net", "smartrecruiters.com", "successfactors",
        "bamboohr.com", "jobvite.com", "ashbyhq.com",
    )
    for key in (
        "applyRedirectUrl", "externalApplyLink", "applyLink", "redirectLink",
        "companyCareerLink", "applyUrl", "externalApplyUrl", "extApplyUrl",
        "externalUrl", "careerPageUrl",
    ):
        val = data.get(key, "")
        if isinstance(val, str) and val.startswith("http"):
            if any(d in val for d in _ATS_DOMAINS):
                return val
    # Broader scan: any string value in the dict containing an ATS domain
    for v in data.values():
        if isinstance(v, str) and v.startswith("http"):
            if any(d in v for d in _ATS_DOMAINS):
                return v
    return ""


# ---------------------------------------------------------------------------
# Indeed — RSS feed (free, no auth, no ScraperAPI)
# ---------------------------------------------------------------------------

class IndeedScraper(BaseScraper):
    platform = "indeed"
    RSS_URL    = "https://in.indeed.com/rss"
    SEARCH_URL = "https://in.indeed.com/jobs"   # ScraperAPI fallback

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for role in roles:
            query = f"{role} {seniority_keywords[0]}"
            print(f"  [Agent1/Indeed] RSS search: '{query}' in {location}")
            jobs = self._rss_search(query, location)
            if jobs:
                print(f"  [Agent1/Indeed]   → {len(jobs)} jobs (RSS)")
                postings.extend(jobs)
                time.sleep(self.delay)
                continue

            # RSS failed — try ScraperAPI render if available
            if SCRAPER_API_KEY:
                print("  [Agent1/Indeed] RSS failed — trying ScraperAPI render fallback")
                for p in range(self.max_pages):
                    url = (
                        f"{self.SEARCH_URL}"
                        f"?q={quote_plus(query)}&l={quote_plus(location)}"
                        f"&fromage=30&start={p * 10}"
                    )
                    resp = scraperapi_fetch(url, render=True, country="in")
                    if resp is None or resp.status_code != 200:
                        break
                    fb_jobs = self._extract_html(resp.text)
                    if not fb_jobs:
                        _debug_dump_html("indeed", resp.text, p + 1)
                        break
                    print(f"  [Agent1/Indeed]   → {len(fb_jobs)} jobs (ScraperAPI page {p+1})")
                    postings.extend(fb_jobs)
                    time.sleep(self.delay)
            else:
                print("  [Agent1/Indeed] RSS failed, no SCRAPER_API_KEY — skipping.")
        return postings

    def _rss_search(self, query: str, location: str) -> list[JobPosting]:
        params = {
            "q":       query,
            "l":       location,
            "fromage": 30,
            "sort":    "date",
        }
        try:
            resp = requests.get(
                self.RSS_URL,
                params=params,
                headers={
                    "User-Agent":      _DEFAULT_UA,
                    "Accept":          "application/rss+xml, application/xml, text/xml",
                    "Accept-Language": "en-IN,en;q=0.9",
                },
                timeout=20,
            )
            if not resp.ok:
                print(f"  [Agent1/Indeed] RSS HTTP {resp.status_code}")
                return []
            return self._parse_rss(resp.text, location)
        except Exception as exc:
            print(f"  [Agent1/Indeed] RSS error: {exc}")
            return []

    def _parse_rss(self, xml_text: str, location: str) -> list[JobPosting]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            print(f"  [Agent1/Indeed] RSS XML parse error: {exc}")
            return []

        ns = {"": ""}
        results: list[JobPosting] = []
        for item in root.findall(".//item"):
            title_el   = item.find("title")
            link_el    = item.find("link")
            desc_el    = item.find("description")
            author_el  = item.find("author")   # Indeed puts company in author

            if title_el is None:
                continue

            raw_title = title_el.text or ""
            # Indeed RSS title format: "Job Title - Company - Location"
            parts     = [p.strip() for p in raw_title.split(" - ")]
            title     = parts[0] if parts else raw_title
            company   = parts[1] if len(parts) > 1 else (author_el.text or "Unknown" if author_el else "Unknown")
            job_loc   = parts[2] if len(parts) > 2 else location

            link = link_el.text or "" if link_el is not None else ""
            # Strip tracking params — keep only the base URL
            link = link.split("?")[0] if "?" in link else link

            desc = ""
            if desc_el is not None and desc_el.text:
                desc = BeautifulSoup(desc_el.text, "html.parser").get_text("\n")[:500]

            # Extract job ID from URL (/viewjob?jk=...)
            jk_match = re.search(r"jk=([a-f0-9]+)", link_el.text or "" if link_el else "")
            job_id   = jk_match.group(1) if jk_match else ""

            results.append(JobPosting(
                platform="indeed",
                title=title, company=company, location=job_loc,
                url=link_el.text or "" if link_el else "",
                jd_text=desc, job_id=job_id,
            ))
        return results

    def _extract_html(self, html: str) -> list[JobPosting]:
        """ScraperAPI fallback HTML parser."""
        soup = BeautifulSoup(html, "html.parser")
        seen_jk: set = set()
        results: list[JobPosting] = []
        for link in soup.find_all("a", {"data-jk": True}):
            jk = link.get("data-jk", "")
            if not jk or jk in seen_jk:
                continue
            seen_jk.add(jk)
            href = link.get("href", "")
            if href and not href.startswith("http"):
                href = "https://in.indeed.com" + href
            title_span = link.find("span", {"title": True}) or link.find("span")
            title = ""
            if title_span:
                title = title_span.get("title") or title_span.get_text(strip=True)
            if not title:
                title = link.get_text(strip=True)
            if not title:
                continue
            company, location = "Unknown", "Pune"
            node = link.parent
            for _ in range(10):
                if node is None:
                    break
                company_el = node.find(attrs={"data-testid": "company-name"})
                if company_el:
                    company = company_el.get_text(strip=True)
                    loc_el = node.find(attrs={"data-testid": "text-location"})
                    if loc_el:
                        location = loc_el.get_text(strip=True)
                    break
                node = node.parent
            results.append(JobPosting(
                platform="indeed", title=title, company=company,
                location=location, url=href, job_id=jk,
            ))
        return results


# ---------------------------------------------------------------------------
# LinkedIn — public guest jobs API (no login, no ScraperAPI needed)
# ---------------------------------------------------------------------------

class LinkedInScraper(BaseScraper):
    platform   = "linkedin"
    GUEST_API  = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for role in roles:
            query = f"{role} {seniority_keywords[0]}"
            for p in range(self.max_pages):
                url = (
                    f"{self.GUEST_API}"
                    f"?keywords={quote_plus(query)}&location={quote_plus(location)}"
                    f"&f_E=4,5&f_TPR=r2592000&start={p * 10}"
                )
                print(f"  [Agent1/LinkedIn] Page {p + 1}: {url}")
                # Try direct request first (guest endpoint, no Akamai)
                resp = self._direct_fetch(url)
                if resp is None or resp.status_code != 200:
                    if SCRAPER_API_KEY:
                        resp = scraperapi_fetch(url, render=False, country="us")
                    if resp is None or resp.status_code != 200:
                        break
                jobs = self._extract(resp.text, location)
                print(f"  [Agent1/LinkedIn]   → {len(jobs)} jobs")
                if not jobs:
                    _debug_dump_html("linkedin", resp.text, p + 1)
                    break
                postings.extend(jobs)
                time.sleep(self.delay)
        return postings

    def _direct_fetch(self, url: str) -> Optional[requests.Response]:
        try:
            return requests.get(
                url,
                headers={
                    "User-Agent":      _DEFAULT_UA,
                    "Accept":          "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer":         "https://www.linkedin.com/",
                },
                timeout=25,
            )
        except requests.RequestException as exc:
            print(f"  [Agent1/LinkedIn] Direct fetch error: {exc}")
            return None

    def _extract(self, html: str, location: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("div.base-card, div.job-search-card")
        if not cards:
            cards = soup.find_all("li")
        results = []
        for card in cards:
            title_el = card.select_one(".base-search-card__title")
            if not title_el:
                continue
            company_el = card.select_one(".base-search-card__subtitle")
            loc_el     = card.select_one(".job-search-card__location")
            link_el    = (
                card.select_one("a.base-card__full-link") or
                card.select_one("a[href*='/jobs/view/']")
            )
            href = link_el.get("href", "").split("?")[0] if link_el else ""
            results.append(JobPosting(
                platform="linkedin",
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href,
            ))
        return results


# ---------------------------------------------------------------------------
# Shared JSON helpers
# ---------------------------------------------------------------------------

def _deep_find_jobdetails(obj, depth: int = 0):
    """Recursively locate a jobDetails list anywhere in a JSON tree."""
    if depth > 8:
        return []
    if isinstance(obj, dict):
        jd = obj.get("jobDetails")
        if isinstance(jd, list) and jd:
            return jd
        for v in obj.values():
            found = _deep_find_jobdetails(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_jobdetails(v, depth + 1)
            if found:
                return found
    return []


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class JobDiscoveryAgent:
    def __init__(self, config: dict):
        self.config = config

    def discover(self) -> list[JobPosting]:
        search     = self.config.get("search", {})
        roles      = search.get("roles", ["Product Owner", "Business Analyst"])
        seniority  = search.get("seniority", ["VP"])
        location   = search.get("location", "Pune")
        platforms  = search.get("platforms", {})

        mode = "free APIs only (no SCRAPER_API_KEY)" if not SCRAPER_API_KEY else "free APIs + ScraperAPI fallback"
        print(f"[Agent1] Starting job discovery — roles: {roles}, seniority: {seniority}, "
              f"location: {location}, mode: {mode}")

        all_postings: list[JobPosting] = []
        scrapers = [
            ("naukri",   NaukriScraper),
            ("indeed",   IndeedScraper),
            ("linkedin", LinkedInScraper),
        ]
        for name, scraper_cls in scrapers:
            if not platforms.get(name, True):
                continue
            try:
                jobs = scraper_cls(self.config).scrape(roles, seniority, location)
                print(f"  [Agent1] {scraper_cls.__name__} found {len(jobs)} postings")
                all_postings.extend(jobs)
            except Exception as exc:
                import traceback
                print(f"  [Agent1] {name} scraper error: {exc}")
                traceback.print_exc()

        unique = self._deduplicate(all_postings)
        print(f"[Agent1] Discovery complete — {len(unique)} unique postings")
        return unique

    @staticmethod
    def _deduplicate(postings: list[JobPosting]) -> list[JobPosting]:
        seen: set = set()
        result: list[JobPosting] = []
        for p in postings:
            key = (p.platform, p.company.lower().strip(), p.title.lower().strip())
            if key not in seen:
                seen.add(key)
                result.append(p)
        return result
