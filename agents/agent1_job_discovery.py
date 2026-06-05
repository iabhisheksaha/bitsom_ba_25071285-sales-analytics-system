"""
Agent 1: Job Discovery and Scraping
Searches LinkedIn, Indeed, and Naukri.com for VP-level Product Owner /
Business Analyst roles in Pune and returns structured job postings.

Architecture
------------
All three sites sit behind anti-bot systems (Akamai on Naukri, Cloudflare on
Indeed, an auth-wall on LinkedIn) that fingerprint and block headless browsers
— including a headless Chromium routed through a proxy. Rotating proxy IPs or
tiers does NOT defeat a *browser-fingerprint* block.

The robust fix is to let ScraperAPI do the scraping server-side: we hit
ScraperAPI's **API endpoint** (api.scraperapi.com) with `render=true`, and
ScraperAPI runs its own anti-bot-hardened browser, solves the challenge, and
returns clean rendered HTML. We then parse that HTML with BeautifulSoup. No
local Playwright, no exposed headless fingerprint.

Credit costs (all available on the free/hobby plan):
    render=true     → ~10 credits/request (JS rendering + anti-bot)
    country_code=in → free (geotargeting), no upgrade required
    premium/ultra   → NOT used (those require a paid plan)
"""

import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SCRAPER_API_ENDPOINT = "https://api.scraperapi.com/"

# Render requests run a full browser on ScraperAPI's side and can take a while.
_RENDER_TIMEOUT = 75
_PLAIN_TIMEOUT = 45

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


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
# ScraperAPI API-endpoint fetch (server-side rendering + anti-bot bypass)
# ---------------------------------------------------------------------------

def scraperapi_fetch(
    target_url: str,
    render: bool = True,
    country: str = "in",
    timeout: Optional[int] = None,
) -> Optional[requests.Response]:
    """
    Fetch ``target_url`` through ScraperAPI's API endpoint.

    ScraperAPI renders the page server-side (when render=True) using its own
    anti-bot browser pool, so we receive the fully-rendered HTML and never
    expose a local headless fingerprint to the target site.

    Returns the requests.Response (status 200 on success) or None on a
    transport-level failure.
    """
    if timeout is None:
        timeout = _RENDER_TIMEOUT if render else _PLAIN_TIMEOUT

    if not SCRAPER_API_KEY:
        # No key (e.g. local dev) — best-effort direct fetch. Will usually be
        # blocked by the target, but keeps the pipeline runnable offline.
        try:
            return requests.get(
                target_url,
                headers={"User-Agent": _DEFAULT_UA, "Accept-Language": "en-IN,en;q=0.9"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            print(f"  [Agent1] Direct fetch failed for {target_url}: {exc}")
            return None

    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "country_code": country,
        "device_type": "desktop",
    }
    if render:
        params["render"] = "true"

    try:
        resp = requests.get(SCRAPER_API_ENDPOINT, params=params, timeout=timeout)
        if resp.status_code != 200:
            body = (resp.text or "")[:300]
            print(f"  [Agent1] ScraperAPI HTTP {resp.status_code} for {target_url}\n"
                  f"           body: {body}")
        return resp
    except requests.RequestException as exc:
        print(f"  [Agent1] ScraperAPI request failed for {target_url}: {exc}")
        return None


def _debug_dump_html(platform: str, html: str, page_no: int = 1) -> None:
    """Persist a fetched page to logs/debug/ so we can diagnose 0-result runs."""
    debug_dir = Path("logs/debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        out = debug_dir / f"{platform}_page{page_no}.html"
        out.write_text(html or "", encoding="utf-8")
        snippet = (html or "")[:600]
        print(f"  [Agent1/{platform.title()}] DEBUG saved {out} ({len(html or '')} chars)")
        print(f"  [Agent1/{platform.title()}] snippet: {snippet}")
    except Exception as exc:  # noqa: BLE001 - debug helper must never crash the run
        print(f"  [Agent1/{platform.title()}] DEBUG dump failed: {exc}")


# ---------------------------------------------------------------------------
# Base scraper
# ---------------------------------------------------------------------------

class BaseScraper:
    platform = "base"

    def __init__(self, config: dict):
        self.config = config
        scraping = config.get("scraping", {})
        self.delay = scraping.get("request_delay", 3)
        # Render requests cost ~10 credits each, so cap pages aggressively to
        # stay within the free-plan credit budget regardless of config value.
        self.max_pages = min(scraping.get("max_pages_per_platform", 5), 2)

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Indeed — ScraperAPI render + Indian IP
# ---------------------------------------------------------------------------

class IndeedScraper(BaseScraper):
    platform = "indeed"
    SEARCH_URL = "https://in.indeed.com/jobs"

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for role in roles:
            query = f"{role} {seniority_keywords[0]}"
            for p in range(self.max_pages):
                url = (
                    f"{self.SEARCH_URL}"
                    f"?q={quote_plus(query)}&l={quote_plus(location)}"
                    f"&fromage=30&start={p * 10}"
                )
                print(f"  [Agent1/Indeed] Page {p + 1}: {url}")
                resp = scraperapi_fetch(url, render=True, country="in")
                if resp is None or resp.status_code != 200:
                    break
                jobs = self._extract(resp.text)
                print(f"  [Agent1/Indeed]   → {len(jobs)} jobs")
                if not jobs:
                    _debug_dump_html("indeed", resp.text, p + 1)
                    break
                postings.extend(jobs)
                time.sleep(self.delay)
        return postings

    def _extract(self, html: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "html.parser")
        # Indeed's job links always carry a data-jk attribute (job key).
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

            company = "Unknown"
            location = "Pune"
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
                platform="indeed",
                title=title, company=company, location=location,
                url=href, job_id=jk,
            ))
        return results


# ---------------------------------------------------------------------------
# Naukri — ScraperAPI render + Indian IP, __NEXT_DATA__ extraction
# ---------------------------------------------------------------------------

class NaukriScraper(BaseScraper):
    platform = "naukri"
    BASE_URL = "https://www.naukri.com"

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for role in roles:
            keyword = f"{role} {seniority_keywords[0]}"
            for p in range(1, self.max_pages + 1):
                slug = re.sub(r"\s+", "-", keyword.lower())
                loc = location.lower()
                url = f"{self.BASE_URL}/{slug}-jobs-in-{loc}-{p}"
                print(f"  [Agent1/Naukri] Page {p}: {url}")
                resp = scraperapi_fetch(url, render=True, country="in")
                if resp is None or resp.status_code != 200:
                    break
                jobs = self._extract(resp.text, role)
                print(f"  [Agent1/Naukri]   → {len(jobs)} jobs")
                if not jobs:
                    _debug_dump_html("naukri", resp.text, p)
                    break
                postings.extend(jobs)
                time.sleep(self.delay)
        return postings

    def _extract(self, html: str, role: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "html.parser")

        # ── Try Next.js data store first (fastest, most complete) ──────────
        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            try:
                data = json.loads(script.string)
                job_list = _deep_find_jobdetails(data)
                if job_list:
                    print(f"  [Agent1/Naukri] __NEXT_DATA__ found {len(job_list)} job objects")
                    return [self._parse_job_obj(j) for j in job_list if j.get("title")]
                print("  [Agent1/Naukri] __NEXT_DATA__ present but no jobDetails array")
            except Exception as exc:  # noqa: BLE001
                print(f"  [Agent1/Naukri] __NEXT_DATA__ parse error: {exc}")

        # ── Fall back to HTML DOM ──────────────────────────────────────────
        return self._parse_html(soup, role)

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
        )

    def _parse_html(self, soup: BeautifulSoup, role: str) -> list[JobPosting]:
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


def _deep_find_jobdetails(obj, depth: int = 0):
    """Recursively locate a ``jobDetails`` list anywhere in the JSON tree."""
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
# LinkedIn — public guest jobs API (raw HTML cards, no login, no JS needed)
# ---------------------------------------------------------------------------

class LinkedInScraper(BaseScraper):
    platform = "linkedin"
    # The guest endpoint returns server-rendered <li> job cards with no auth
    # wall and no client-side JS — far more reliable than the full SPA page.
    GUEST_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

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
                # Guest cards are static HTML — render not needed (saves credits).
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

    def _extract(self, html: str, location: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "html.parser")
        # The guest endpoint returns one card per job; prefer the card div, and
        # fall back to bare <li> wrappers only if no card class is present.
        cards = soup.select("div.base-card, div.job-search-card")
        if not cards:
            cards = soup.find_all("li")
        results = []
        for card in cards:
            title_el = card.select_one(".base-search-card__title")
            if not title_el:
                continue
            company_el = card.select_one(".base-search-card__subtitle")
            loc_el = card.select_one(".job-search-card__location")
            link_el = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
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
# Orchestrator for Agent 1
# ---------------------------------------------------------------------------

class JobDiscoveryAgent:
    def __init__(self, config: dict):
        self.config = config

    def discover(self) -> list[JobPosting]:
        search = self.config.get("search", {})
        roles = search.get("roles", ["Product Owner", "Business Analyst"])
        seniority = search.get("seniority", ["VP"])
        location = search.get("location", "Pune")
        platforms = search.get("platforms", {})

        print(
            f"[Agent1] Starting job discovery — roles: {roles}, "
            f"seniority: {seniority}, location: {location}"
        )
        if SCRAPER_API_KEY:
            print("  [Agent1] ScraperAPI render mode active (server-side anti-bot browser)")
        else:
            print("  [Agent1] No SCRAPER_API_KEY — attempting direct fetches (likely blocked)")

        all_postings: list[JobPosting] = []
        scrapers = [
            ("indeed", IndeedScraper),
            ("naukri", NaukriScraper),
            ("linkedin", LinkedInScraper),
        ]
        for name, scraper_cls in scrapers:
            if not platforms.get(name, True):
                continue
            try:
                jobs = scraper_cls(self.config).scrape(roles, seniority, location)
                print(f"  [Agent1] {scraper_cls.__name__} found {len(jobs)} postings")
                all_postings.extend(jobs)
            except Exception as exc:  # noqa: BLE001 - one site failing must not abort others
                import traceback
                print(f"  [Agent1] {name} scraper error: {exc}")
                traceback.print_exc()

        unique = self._deduplicate(all_postings)
        print(f"[Agent1] Discovery complete — {len(unique)} unique postings")
        return unique

    @staticmethod
    def _deduplicate(postings: list[JobPosting]) -> list[JobPosting]:
        seen: set = set()
        unique: list[JobPosting] = []
        for p in postings:
            key = (p.company.lower().strip(), p.title.lower().strip())
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique
