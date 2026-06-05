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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

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

_CHROMIUM_BIN = os.environ.get(
    "CHROMIUM_BIN",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
)

def _launch_scraper_browser(pw, use_proxy: bool = True):
    chromium_bin = _CHROMIUM_BIN if Path(_CHROMIUM_BIN).exists() else None
    launch_kwargs = dict(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
              "--ignore-certificate-errors"],
    )
    if chromium_bin:
        launch_kwargs["executable_path"] = chromium_bin
    if use_proxy and SCRAPER_API_KEY:
        launch_kwargs["proxy"] = {
            "server":   "http://proxy-server.scraperapi.com:8001",
            "username": "scraperapi",
            "password": SCRAPER_API_KEY,
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
    BASE_URL   = "https://www.naukri.com"
    LOGIN_URL  = "https://www.naukri.com/login"

    def __init__(self, config: dict, browser):
        self.config = config
        self.browser = browser
        self.max_pages = config.get("scraping", {}).get("max_pages_per_platform", 5)
        self.delay = config.get("scraping", {}).get("request_delay", 3)

    def _try_login(self, page) -> bool:
        """
        Attempt Naukri login using credentials from the encrypted store.
        Returns True on success, False if credentials unavailable or login failed.
        """
        try:
            from agents.agent4_credentials import CredentialManager, load_key_from_env
            key  = load_key_from_env("CRED_KEY")
            mgr  = CredentialManager(self.config.get("credentials", {}).get("encrypted_file", "config/credentials.enc"))
            cred = mgr.get_site_credentials("naukri", key)
            username = cred.get("username", "")
            password = cred.get("password", "")
            if not username or not password:
                return False
        except Exception as exc:
            print(f"  [Agent1/Naukri] Credentials unavailable, scraping as guest: {exc}")
            return False

        try:
            print("  [Agent1/Naukri] Logging in…")
            page.goto(self.LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            print(f"  [Agent1/Naukri] Login page URL: {page.url!r}, title: {page.title()!r}")
            # Email — Naukri uses name="username" or placeholder variations
            email_sel = (
                "input[name='username'], input[type='email'], "
                "input[placeholder*='Email'], input[placeholder*='email'], "
                "#usernameField"
            )
            page.fill(email_sel, username, timeout=10000)
            page.wait_for_timeout(500)
            # Password
            page.fill("input[type='password'], input[name='password']", password, timeout=8000)
            page.wait_for_timeout(500)
            # Submit
            page.click(
                "button[type='submit'], input[type='submit'], "
                "button:has-text('Login'), button:has-text('Sign in')",
                timeout=8000,
            )
            page.wait_for_timeout(3000)
            final_url = page.url
            if "login" not in final_url:
                print(f"  [Agent1/Naukri] Login successful → {final_url!r}")
                return True
            print(f"  [Agent1/Naukri] Still on login page after submit — continuing as guest ({final_url!r})")
            return False
        except Exception as exc:
            print(f"  [Agent1/Naukri] Login attempt failed (continuing as guest): {exc}")
            return False

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        ctx = self.browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-IN",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        page = ctx.new_page()
        try:
            self._try_login(page)
            for role in roles:
                keyword = f"{role} {seniority_keywords[0]}"
                for p in range(1, self.max_pages + 1):
                    slug = re.sub(r"\s+", "-", keyword.lower())
                    loc  = location.lower()
                    url  = f"{self.BASE_URL}/{slug}-jobs-in-{loc}-{p}"
                    print(f"  [Agent1/Naukri] Page {p}: {url}")
                    try:
                        page.goto(url, timeout=45000, wait_until="domcontentloaded")
                        # Wait for either job cards or the Next.js data script to appear
                        try:
                            page.wait_for_selector(
                                "#__NEXT_DATA__, article.srp-jobtuple-wrapper, [data-job-id]",
                                timeout=20000,
                            )
                        except Exception:
                            pass  # fall through to debug dump below
                        final_url = page.url
                        if final_url != url:
                            print(f"  [Agent1/Naukri] Redirected → {final_url!r}")
                        page.wait_for_timeout(2000)
                        jobs = self._extract(page, role)
                        print(f"  [Agent1/Naukri]   → {len(jobs)} jobs")
                        if not jobs:
                            self._debug_dump(page, "naukri")
                            break
                        postings.extend(jobs)
                        time.sleep(self.delay)
                    except Exception as exc:
                        import traceback
                        print(f"  [Agent1/Naukri] Error on page {p}: {exc}")
                        traceback.print_exc()
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
                # Naukri has changed the key path several times; try all known variants
                job_list = (
                    props.get("jobDetails") or
                    props.get("jobResults", {}).get("jobDetails") or
                    props.get("data", {}).get("jobDetails") or
                    props.get("initialState", {}).get("jobDetails") or
                    []
                )
                # Also check top-level props for alternate structures
                if not job_list:
                    def _deep_find(obj, depth=0):
                        if depth > 6 or not isinstance(obj, dict):
                            return []
                        if "jobDetails" in obj and isinstance(obj["jobDetails"], list):
                            return obj["jobDetails"]
                        for v in obj.values():
                            found = _deep_find(v, depth + 1)
                            if found:
                                return found
                        return []
                    job_list = _deep_find(data)

                if job_list:
                    print(f"  [Agent1/Naukri] __NEXT_DATA__ found {len(job_list)} job objects")
                    return [self._parse_job_obj(j) for j in job_list if j.get("title")]
                else:
                    print("  [Agent1/Naukri] __NEXT_DATA__ present but no jobDetails array found")
        except Exception as exc:
            print(f"  [Agent1/Naukri] __NEXT_DATA__ parse error: {exc}")

        # ── Fall back to HTML DOM ──────────────────────────────────────────
        results = self._parse_html(page.content(), role)
        if not results:
            print("  [Agent1/Naukri] HTML DOM fallback also found 0 cards")
        return results

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

    def _debug_dump(self, page, platform: str) -> None:
        """Save page title + HTML snippet + screenshot to logs/debug/ for diagnosis."""
        import traceback
        debug_dir = Path("logs/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            title = page.title()
            print(f"  [Agent1/{platform.title()}] DEBUG page title: {title!r}")
        except Exception:
            title = "(unknown)"
        try:
            html = page.content()
            snippet = html[:4000]
            out_html = debug_dir / f"{platform}_page.html"
            out_html.write_text(html, encoding="utf-8")
            print(f"  [Agent1/{platform.title()}] DEBUG HTML saved → {out_html} ({len(html)} chars)")
            print(f"  [Agent1/{platform.title()}] HTML snippet:\n{snippet[:800]}")
        except Exception as exc:
            print(f"  [Agent1/{platform.title()}] DEBUG html dump failed: {exc}")
        try:
            shot = debug_dir / f"{platform}_screenshot.png"
            page.screenshot(path=str(shot), full_page=False)
            print(f"  [Agent1/{platform.title()}] DEBUG screenshot → {shot}")
        except Exception as exc:
            print(f"  [Agent1/{platform.title()}] DEBUG screenshot failed: {exc}")

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
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            },
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
                        page.goto(url, timeout=45000, wait_until="domcontentloaded")
                        try:
                            page.wait_for_selector(
                                "div.job_seen_beacon, [data-jk], #mosaic-jobResults",
                                timeout=15000,
                            )
                        except Exception:
                            pass
                        page.wait_for_timeout(2000)
                        jobs = self._extract(page)
                        print(f"  [Agent1/Indeed]   → {len(jobs)} jobs")
                        if not jobs:
                            self._debug_dump(page, "indeed")
                            break
                        postings.extend(jobs)
                        time.sleep(self.delay)
                    except Exception as exc:
                        import traceback
                        print(f"  [Agent1/Indeed] Error on page {p + 1}: {exc}")
                        traceback.print_exc()
                        break
        finally:
            ctx.close()
        return postings

    def _debug_dump(self, page, platform: str) -> None:
        debug_dir = Path("logs/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            title = page.title()
            print(f"  [Agent1/{platform.title()}] DEBUG page title: {title!r}")
        except Exception:
            title = "(unknown)"
        try:
            html = page.content()
            out_html = debug_dir / f"{platform}_page.html"
            out_html.write_text(html, encoding="utf-8")
            print(f"  [Agent1/{platform.title()}] DEBUG HTML saved → {out_html} ({len(html)} chars)")
            print(f"  [Agent1/{platform.title()}] HTML snippet:\n{html[:800]}")
        except Exception as exc:
            print(f"  [Agent1/{platform.title()}] DEBUG html dump failed: {exc}")
        try:
            shot = debug_dir / f"{platform}_screenshot.png"
            page.screenshot(path=str(shot), full_page=False)
            print(f"  [Agent1/{platform.title()}] DEBUG screenshot → {shot}")
        except Exception as exc:
            print(f"  [Agent1/{platform.title()}] DEBUG screenshot failed: {exc}")

    def _extract(self, page) -> list[JobPosting]:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Indeed's job links always carry a data-jk attribute (job key).
        # Walk UP from each link to find company/location in the enclosing card.
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

            # Title is in a span[title] or the first span inside the <a>
            title_span = link.find("span", {"title": True}) or link.find("span")
            title = ""
            if title_span:
                title = title_span.get("title") or title_span.get_text(strip=True)
            if not title:
                title = link.get_text(strip=True)
            if not title:
                continue

            # Walk up to find the card container holding company / location
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
                title=title,
                company=company,
                location=location,
                url=href,
                job_id=jk,
            ))

        return results


# ---------------------------------------------------------------------------
# LinkedIn scraper  — Playwright (requests guest API returns 403)
# ---------------------------------------------------------------------------

class LinkedInScraper:
    SEARCH_URL = "https://www.linkedin.com/jobs/search"

    def __init__(self, config: dict, browser):
        self.config = config
        self.browser = browser
        self.max_pages = config.get("scraping", {}).get("max_pages_per_platform", 5)
        self.delay = config.get("scraping", {}).get("request_delay", 3)

    def _try_login(self, page) -> bool:
        try:
            from agents.agent4_credentials import CredentialManager, load_key_from_env
            key  = load_key_from_env("CRED_KEY")
            mgr  = CredentialManager(self.config.get("credentials", {}).get("encrypted_file", "config/credentials.enc"))
            cred = mgr.get_site_credentials("linkedin", key)
            username = cred.get("username", "")
            password = cred.get("password", "")
            if not username or not password:
                return False
        except Exception as exc:
            print(f"  [Agent1/LinkedIn] Credentials unavailable, scraping public listings: {exc}")
            return False

        try:
            print("  [Agent1/LinkedIn] Logging in…")
            page.goto("https://www.linkedin.com/login", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            page.fill("input[name='session_key'], #username", username, timeout=10000)
            page.fill("input[name='session_password'], #password", password, timeout=8000)
            page.click("button[type='submit'], button[data-litms-control-urn*='login']", timeout=8000)
            page.wait_for_timeout(4000)
            final_url = page.url
            if "feed" in final_url or "jobs" in final_url or "linkedin.com/in/" in final_url:
                print(f"  [Agent1/LinkedIn] Login successful → {final_url!r}")
                return True
            print(f"  [Agent1/LinkedIn] Login may have failed ({final_url!r}), continuing anyway")
            return False
        except Exception as exc:
            print(f"  [Agent1/LinkedIn] Login failed (continuing as guest): {exc}")
            return False

    def scrape(self, roles: list, seniority_keywords: list, location: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        ctx = self.browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = ctx.new_page()
        try:
            self._try_login(page)
            for role in roles:
                query = f"{role} {seniority_keywords[0]}"
                for p in range(self.max_pages):
                    url = (
                        f"{self.SEARCH_URL}"
                        f"?keywords={quote_plus(query)}"
                        f"&location={quote_plus(location)}"
                        f"&f_E=4,5&sortBy=DD&position=1&pageNum={p}"
                    )
                    print(f"  [Agent1/LinkedIn] Page {p + 1}: {url}")
                    try:
                        page.goto(url, timeout=45000, wait_until="domcontentloaded")
                        try:
                            page.wait_for_selector(
                                "ul.jobs-search__results-list, .base-card, .job-search-card",
                                timeout=15000,
                            )
                        except Exception:
                            pass
                        page.wait_for_timeout(2000)
                        jobs = self._extract(page, location)
                        print(f"  [Agent1/LinkedIn]   → {len(jobs)} jobs")
                        if not jobs:
                            self._debug_dump(page, "linkedin")
                            break
                        postings.extend(jobs)
                        time.sleep(self.delay)
                    except Exception as exc:
                        import traceback
                        print(f"  [Agent1/LinkedIn] Error on page {p + 1}: {exc}")
                        traceback.print_exc()
                        break
        finally:
            ctx.close()
        return postings

    def _debug_dump(self, page, platform: str) -> None:
        debug_dir = Path("logs/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            title = page.title()
            print(f"  [Agent1/{platform.title()}] DEBUG title: {title!r}")
        except Exception:
            pass
        try:
            html = page.content()
            (debug_dir / f"{platform}_page.html").write_text(html, encoding="utf-8")
            print(f"  [Agent1/{platform.title()}] DEBUG HTML saved ({len(html)} chars)")
        except Exception:
            pass
        try:
            page.screenshot(path=str(debug_dir / f"{platform}_screenshot.png"), full_page=False)
            print(f"  [Agent1/{platform.title()}] DEBUG screenshot saved")
        except Exception:
            pass

    def _extract(self, page, location: str) -> list[JobPosting]:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for card in soup.select("li.jobs-search__results-list > *, .base-card, .job-search-card"):
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
        return results


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

        # ── Naukri — direct (no proxy; ScraperAPI IPs are blocked by Naukri) ─
        if platforms.get("naukri", True):
            try:
                from playwright.sync_api import sync_playwright
                print("  [Agent1] Launching Playwright browser for Naukri (no proxy)…")
                with sync_playwright() as pw:
                    browser = _launch_scraper_browser(pw, use_proxy=False)
                    try:
                        jobs = NaukriScraper(self.config, browser).scrape(roles, seniority, location)
                        print(f"  [Agent1] NaukriScraper found {len(jobs)} postings")
                        all_postings.extend(jobs)
                    finally:
                        browser.close()
            except Exception as exc:
                import traceback
                print(f"  [Agent1] Naukri Playwright error: {exc}")
                traceback.print_exc()

        # ── Indeed + LinkedIn — via ScraperAPI proxy ───────────────────────
        needs_proxy_pw = platforms.get("indeed", True) or platforms.get("linkedin", True)
        if needs_proxy_pw:
            try:
                from playwright.sync_api import sync_playwright
                print("  [Agent1] Launching Playwright browser for Indeed/LinkedIn (proxy)…")
                with sync_playwright() as pw:
                    browser = _launch_scraper_browser(pw, use_proxy=True)
                    try:
                        if platforms.get("indeed", True):
                            jobs = IndeedScraper(self.config, browser).scrape(roles, seniority, location)
                            print(f"  [Agent1] IndeedScraper found {len(jobs)} postings")
                            all_postings.extend(jobs)
                        if platforms.get("linkedin", True):
                            jobs = LinkedInScraper(self.config, browser).scrape(roles, seniority, location)
                            print(f"  [Agent1] LinkedInScraper found {len(jobs)} postings")
                            all_postings.extend(jobs)
                    finally:
                        browser.close()
            except Exception as exc:
                import traceback
                print(f"  [Agent1] Indeed/LinkedIn Playwright error: {exc}")
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
