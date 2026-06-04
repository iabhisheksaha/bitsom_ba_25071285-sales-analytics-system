"""
Agent 1: Job Discovery and Scraping
Searches LinkedIn, Indeed, and Naukri.com for VP-level Product Owner /
Business Analyst roles in Pune and returns structured job postings.

LinkedIn  — requests-based (guest JSON API, no login needed)
Naukri    — Playwright-based (JS-rendered page, session cookies required)
Indeed    — Playwright-based (JS-rendered page, anti-bot on requests)
"""

import os
import random
import re
import time
import urllib3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

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
# Shared browser launcher (reuses Agent 3 config so proxy is consistent)
# ---------------------------------------------------------------------------

def _launch_scraper_browser(pw):
    from agents.agent3_application import CHROMIUM_BIN, SCRAPER_API_KEY as SA_KEY
    chromium_bin = CHROMIUM_BIN if Path(CHROMIUM_BIN).exists() else None
    launch_kwargs = dict(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
              "--ignore-certificate-errors"],
    )
    if chromium_bin:
        launch_kwargs["executable_path"] = chromium_bin
    if SA_KEY:
        launch_kwargs["proxy"] = {
            "server":   "http://proxy-server.scraperapi.com:8001",
            "username": "scraperapi",
            "password": SA_KEY,
        }
    return pw.chromium.launch(**launch_kwargs)


# ---------------------------------------------------------------------------
# Requests-based base scraper (LinkedIn only)
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
# Naukri scraper  — Playwright (handles JS rendering + session cookies)
# ---------------------------------------------------------------------------

class NaukriScraper:
    BASE_URL = "https://www.naukri.com"

    def __init__(self, config: dict, browser):
        self.config = config
        self.browser = browser
        self.max_pages = config.get("scraping", {}).get("max_pages_per_platform", 5)
        self.delay = config.get("scraping", {}).get("request_delay", 3)

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        ctx = self.browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-IN",
        )
        page = ctx.new_page()
        try:
            for role in roles:
                keyword = f"{role} {seniority_keywords[0]}"
                for p in range(1, self.max_pages + 1):
                    slug = re.sub(r"\s+", "-", keyword.lower())
                    loc  = location.lower()
                    url  = f"{self.BASE_URL}/{slug}-jobs-in-{loc}-{p}"
                    print(f"  [Agent1/Naukri] Page {p}: {url}")
                    try:
                        page.goto(url, timeout=30000, wait_until="domcontentloaded")
                        page.wait_for_timeout(4000)
                        jobs = self._extract(page, role)
                        print(f"  [Agent1/Naukri]   → {len(jobs)} jobs")
                        if not jobs:
                            break
                        postings.extend(jobs)
                        time.sleep(self.delay)
                    except Exception as exc:
                        print(f"  [Agent1/Naukri] Error on page {p}: {exc}")
                        break
        finally:
            ctx.close()
        return postings

    def _extract(self, page, role: str) -> list[JobPosting]:
        # ── Try Next.js data store first (fastest, most complete) ──────────
        try:
            data = page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    if (!el) return null;
                    try { return JSON.parse(el.textContent); } catch { return null; }
                }
            """)
            if data:
                props = data.get("props", {}).get("pageProps", {})
                job_list = (
                    props.get("jobDetails") or
                    props.get("jobResults", {}).get("jobDetails") or
                    props.get("data", {}).get("jobDetails") or []
                )
                if job_list:
                    return [self._parse_job_obj(j) for j in job_list if j.get("title")]
        except Exception:
            pass

        # ── Fall back to HTML DOM ──────────────────────────────────────────
        return self._parse_html(page.content(), role)

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

    def _parse_html(self, html: str, role: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "html.parser")
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
            loc_el     = card.select_one("span.locWdth") or card.select_one("[class*='location']")
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


# ---------------------------------------------------------------------------
# Indeed scraper  — Playwright (403 on requests even with residential IP)
# ---------------------------------------------------------------------------

class IndeedScraper:
    SEARCH_URL = "https://in.indeed.com/jobs"

    def __init__(self, config: dict, browser):
        self.config = config
        self.browser = browser
        self.max_pages = config.get("scraping", {}).get("max_pages_per_platform", 5)
        self.delay = config.get("scraping", {}).get("request_delay", 3)

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        ctx = self.browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()
        try:
            for role in roles:
                query = f"{role} {seniority_keywords[0]}"
                for p in range(self.max_pages):
                    url = (
                        f"{self.SEARCH_URL}"
                        f"?q={quote_plus(query)}&l={quote_plus(location)}"
                        f"&fromage=30&start={p * 10}"
                    )
                    print(f"  [Agent1/Indeed] Page {p + 1}: {url}")
                    try:
                        page.goto(url, timeout=30000, wait_until="domcontentloaded")
                        page.wait_for_timeout(4000)
                        jobs = self._extract(page)
                        print(f"  [Agent1/Indeed]   → {len(jobs)} jobs")
                        if not jobs:
                            break
                        postings.extend(jobs)
                        time.sleep(self.delay)
                    except Exception as exc:
                        print(f"  [Agent1/Indeed] Error on page {p + 1}: {exc}")
                        break
        finally:
            ctx.close()
        return postings

    def _extract(self, page) -> list[JobPosting]:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        cards = (
            soup.select("div.job_seen_beacon") or
            soup.select("[data-jk]") or
            soup.select("li[class*='css-']")
        )
        results = []
        for card in cards:
            title_el = (
                card.select_one("h2.jobTitle span[title]") or
                card.select_one("h2.jobTitle span") or
                card.select_one("h2[class*='jobTitle']")
            )
            if not title_el:
                continue
            company_el = (
                card.select_one("[data-testid='company-name']") or
                card.select_one("[class*='companyName']")
            )
            loc_el = (
                card.select_one("[data-testid='text-location']") or
                card.select_one("[class*='companyLocation']")
            )
            link_el = card.select_one("a[href*='/rc/clk']") or card.select_one("h2 a")
            title = title_el.get("title") or title_el.get_text(strip=True)
            href  = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = "https://in.indeed.com" + href
            results.append(JobPosting(
                platform="indeed",
                title=title,
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href,
            ))
        return results


# ---------------------------------------------------------------------------
# LinkedIn scraper  — requests-based guest API (no login, no Playwright)
# ---------------------------------------------------------------------------

class LinkedInScraper(BaseScraper):
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
                    "f_E":      "4,5",
                    "start":    page_num * 25,
                    "sortBy":   "DD",
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
        for card in soup.select("li") or []:
            title_el   = card.select_one(".base-search-card__title")
            company_el = card.select_one(".base-search-card__subtitle")
            loc_el     = card.select_one(".job-search-card__location")
            link_el    = card.select_one("a.base-card__full-link")
            if not title_el:
                continue
            href = link_el.get("href", "") if link_el else ""
            results.append(JobPosting(
                platform="linkedin",
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href,
            ))
            if href:
                results[-1].jd_text = self._fetch_jd(href)
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
    def __init__(self, config: dict):
        self.config = config

    def discover(self) -> list[JobPosting]:
        search    = self.config.get("search", {})
        roles     = search.get("roles", ["Product Owner", "Business Analyst"])
        seniority = search.get("seniority", ["VP"])
        location  = search.get("location", "Pune")
        platforms = search.get("platforms", {})

        print(
            f"[Agent1] Starting job discovery — roles: {roles}, "
            f"seniority: {seniority}, location: {location}"
        )

        all_postings: list[JobPosting] = []

        # ── LinkedIn via requests (guest API) ─────────────────────────────
        if platforms.get("linkedin", True):
            try:
                jobs = LinkedInScraper(self.config).scrape(roles, seniority, location)
                print(f"  [Agent1] LinkedInScraper found {len(jobs)} postings")
                all_postings.extend(jobs)
            except Exception as exc:
                print(f"  [Agent1] LinkedInScraper error: {exc}")

        # ── Naukri + Indeed via Playwright ────────────────────────────────
        needs_pw = platforms.get("naukri", True) or platforms.get("indeed", True)
        if needs_pw:
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    browser = _launch_scraper_browser(pw)
                    try:
                        if platforms.get("naukri", True):
                            jobs = NaukriScraper(self.config, browser).scrape(roles, seniority, location)
                            print(f"  [Agent1] NaukriScraper found {len(jobs)} postings")
                            all_postings.extend(jobs)
                        if platforms.get("indeed", True):
                            jobs = IndeedScraper(self.config, browser).scrape(roles, seniority, location)
                            print(f"  [Agent1] IndeedScraper found {len(jobs)} postings")
                            all_postings.extend(jobs)
                    finally:
                        browser.close()
            except Exception as exc:
                print(f"  [Agent1] Playwright scraping error: {exc}")

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
