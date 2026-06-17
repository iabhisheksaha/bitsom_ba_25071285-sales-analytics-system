# Automated Job Application System — Technical Specification

**Audience**: An AI coding agent (or engineer) rebuilding this system from scratch.
**Scope**: This spec covers the job-application automation pipeline only. The repository
also contains an unrelated academic "Sales Analytics System" assignment
(`main.py`, `utils/file_handler.py`, `utils/data_processor.py`, `utils/api_handler.py`,
`README.md`) — ignore that code when implementing this system; it shares the repo but
not the codebase.

No real secrets, tokens, or passwords appear anywhere in this document. Every credential
is referenced only by its environment-variable name or storage mechanism.

---

## 1. System Overview

A multi-agent pipeline that discovers job postings, filters them for fit using an LLM
ensemble, tailors a resume per job, and submits applications autonomously (or semi-
autonomously, with human-in-the-loop fallback via Telegram for CAPTCHAs and unknown
credentials).

```
Agent 1 (Discovery) → Council Job-Scoring Filter → Agent 2 (Resume Tailoring) → Agent 3 (Application Submission)
                                                                                        ↑
                                                                          Agent 4 (Credentials) — used at runtime
```

Entry point: `orchestrator.py`. Run modes: full pipeline, dry-run (discovery + tailoring
only, no submission), credential setup (interactive or env-based), Telegram connectivity
test.

### 1.1 Design principles to preserve
- **Resilience over elegance**: every external call (scraping, LLM, ATS automation) has a
  fallback chain. Nothing should hard-fail the whole run if one source/provider is down.
- **Human-in-the-loop for ambiguity**: CAPTCHAs, missing credentials, and unknown ATS
  platforms escalate to a human via Telegram rather than guessing or crashing.
- **Bounded LLM usage**: every subsystem that calls an LLM caps the number of calls per
  run/pass to control latency and cost (e.g. form-filling caps at 4 council calls per
  pass; everything else falls back to deterministic heuristics/regex).
- **No secrets in the repo**: all credentials live in environment variables, an encrypted
  file, or the OS credential store — never in tracked files.

---

## 2. Tech Stack

| Concern | Choice |
|---|---|
| Language | Python 3.10+ (uses `tuple[float, str]` style annotations, `"list[X]"` string-quoted generics for back-compat) |
| HTTP (sync) | `requests` |
| HTTP (async) | `httpx>=0.27.0` (LLM Council parallel calls) |
| HTML parsing | `beautifulsoup4>=4.12.0` + `lxml>=5.0.0` |
| Browser automation | `playwright` + `playwright-stealth` |
| Resume documents | `python-docx>=1.1.0` |
| Encryption | `cryptography>=42.0.0` (Fernet symmetric encryption) |
| Config | `pyyaml>=6.0.1` |
| LLM SDK | `anthropic>=0.30.0` (one of three possible LLM backends) |
| CI/Scheduling | GitHub Actions (cron + manual dispatch); optional local Windows Task Scheduler |
| Notifications | Telegram Bot API (`requests`, no SDK) |

---

## 3. Directory Structure

```
orchestrator.py                    # Entry point / pipeline coordinator
agents/
  agent1_job_discovery.py          # Scraping + JobPosting model
  agent2_resume_tailoring.py       # Resume customization per job
  agent3_application.py            # ATS automation + submission
  agent4_credentials.py            # Encrypted credential store
utils/
  llm_council.py                   # Multi-model async deliberation
  job_scorer.py                    # Council-based job fit scoring
  ai_form_filler.py                # Three-tier form-field answering
config/
  config.yaml                      # Main pipeline configuration
  applicant_answers.yaml           # Pre-stored screening-question answers
  credentials.enc                  # Generated at runtime — gitignored, never committed
resume/
  base resume .docx + tailored/    # tailored/ output is gitignored
logs/
  discovered_jobs.json             # Checkpoint, gitignored
  applications.json                # Application history/log, gitignored
  reports/, debug/                 # gitignored
.github/workflows/daily_apply.yml  # Scheduled CI run
run_local.ps1, first_time_setup.ps1, setup_scheduler.ps1   # Windows local-run tooling
test_*.py, mock_run.py             # Offline mocks / regression tests (see §12)
```

---

## 4. Configuration System

### 4.1 `config/config.yaml`
Top-level structure (informed by usage in `orchestrator.py` and agents):

```yaml
search:
  platforms:
    naukri: true
    indeed: true
    linkedin: true
  # keywords, locations, seniority filters consumed by Agent 1

credentials:
  encrypted_file: "config/credentials.enc"

council:
  members:
    - "google/gemini-2.0-flash-exp:free"
    - "meta-llama/llama-3.1-8b-instruct:free"
    - "mistralai/mistral-7b-instruct:free"
    - "qwen/qwen3-8b:free"
  chairman: "google/gemini-2.0-flash-exp:free"
  use_for:
    resume_tailoring: true
    form_filling: true
    job_scoring: true
  min_job_score: 6.0

applicant:
  # optional identity-overlay fields merged on top of applicant_answers.yaml
  name: "..."
  location: "..."
```

`council.members` are model identifiers in **OpenRouter** `provider/model:tag` format when
routed through OpenRouter; the same config block is reused with different model-name
semantics if routed through Google AI Studio directly (see §6).

### 4.2 `config/applicant_answers.yaml`
Static fallback answers for common screening-form questions, organized into commented
sections: identity/contact (city, state, country, zip), current role & experience (years
of experience, years in product/BA roles), compensation (current/expected CTC),
logistics (notice period, relocation, travel, remote preference), work authorization
(sponsorship requirement, visa type), education (degree, university, graduation year),
EEO/demographic (gender, ethnicity, veteran/disability status — defaults to "decline to
self-identify" style answers, left blank-safe), and a free-text `resume_summary` block
that gets injected into LLM prompts for screening questions and resume tailoring.

`utils.ai_form_filler.load_applicant_profile()` merges this YAML (base layer) with
`config.yaml`'s `applicant:` block (identity overlay — any non-empty value in
`config.yaml` wins over the YAML default).

---

## 5. Secrets & Credential Management

**No actual secret values are ever stored in the repository.** Two independent mechanisms
exist depending on where the pipeline runs:

### 5.1 Encrypted on-disk store (`agents/agent4_credentials.py`)
- `CredentialManager` wraps a Fernet-encrypted JSON blob at `config/credentials.enc`
  (path configurable via `config.credentials.encrypted_file`).
- Key API:
  - `CredentialManager.generate_key()` — static method, calls `Fernet.generate_key()`.
  - `init_store(key: bytes, credentials: dict)` — encrypts `{platform: {username, password}}`
    and writes the file with `chmod 600`.
  - `_load_all(key)` — decrypts and parses the JSON.
  - `get_credential(site, field, key)`, `get_site_credentials(site, key)`,
    `list_sites(key)`, `upsert_credential(site, field, value, key)`.
- The Fernet key itself is supplied at runtime via the `CRED_KEY` environment variable —
  it is **never** written next to the encrypted file.
- `config/credentials.enc` is listed in `.gitignore` and must never be committed.

### 5.2 Bootstrapping flows (`orchestrator.py`)
- **Interactive** (`--init-creds`): generates a new Fernet key, prints it once for the
  operator to store externally, then prompts for username/password per enabled platform
  and writes the encrypted store.
- **Non-interactive / CI** (`--setup-creds-from-env`): reads `{PLATFORM}_USER` /
  `{PLATFORM}_PASS` env vars for every platform enabled in `config.search.platforms`,
  writes `credentials.enc`, then **immediately pops those env vars from `os.environ`** so
  plaintext credentials don't linger in the process environment for the rest of the run.
- **Auto-bootstrap**: if `run_pipeline()` finds `CRED_KEY` set but no `credentials.enc` on
  disk, and a `NAUKRI_USER` env var is present (heuristic signal this is a fresh CI
  container), it transparently calls the env-based setup before continuing.

### 5.3 Storage locations by environment
| Environment | Where secrets live |
|---|---|
| GitHub Actions (CI) | Repository secrets, injected as env vars into the workflow step: `CRED_KEY`, `NAUKRI_USER/PASS`, `LINKEDIN_USER/PASS`, `TELEGRAM_BOT_TOKEN/CHAT_ID`, `SCRAPER_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, optionally `ANTHROPIC_API_KEY` |
| Local Windows | OS Credential Manager entries (e.g. `JobApp_CRED_KEY`, `JobApp_TG_BOT_TOKEN`, `JobApp_TG_CHAT_ID`, `JobApp_ANTHROPIC_KEY`, `JobApp_OPENROUTER_KEY`, `JobApp_GOOGLE_KEY`), read by a PowerShell runner script before invoking Python |
| Job-site/platform passwords | Always inside `credentials.enc`, never as raw env vars beyond the one-time bootstrap moment |

A human-in-the-loop fallback also exists: if `Agent3` encounters a platform/ATS with no
stored credentials, it can request them interactively via Telegram (operator replies with
a one-time message), rather than failing the run.

---

## 6. LLM Council (`utils/llm_council.py`)

Implements the **"LLM Council" deliberation pattern** (multi-model ensemble: independent
answers → peer cross-ranking → chairman synthesis), used wherever the pipeline needs
judgment calls (job fit scoring, resume summary rewriting, ambiguous form-field answers).

### 6.1 Backend routing priority
On each call, the module picks **one** backend based on which credentials are present,
in this order:
1. **OpenRouter** (`OPENROUTER_API_KEY`) — free-tier models (`:free` suffixed model IDs),
   no cost, generous rate limits suitable for a daily batch job.
2. **Google AI Studio** (`GOOGLE_API_KEY`) — called via Google's OpenAI-compatible REST
   endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`),
   also free with no credit card required.
3. **Anthropic** (`ANTHROPIC_API_KEY`) — paid, original/reference backend using the
   `anthropic` SDK.
4. **Heuristic fallback** — if no LLM credentials are available at all, every consumer
   (`job_scorer`, `ai_form_filler`, `agent2_resume_tailoring`) degrades to deterministic
   keyword/regex logic instead of failing.

### 6.2 Public surface used by consumers
- `ask_council_brief(question: str, system: str, max_tokens: int) -> str | None` — a
  lightweight single-shot helper (no full 3-stage deliberation) used by `job_scorer` and
  `ai_form_filler` for cheap, fast judgment calls. Returns `None` (triggering fallback) on
  any error or if no backend is configured.
- A fuller multi-stage deliberation entry point (used for resume-summary generation in
  Agent 2) that runs the 3-stage process across `council.members` and synthesizes via
  `council.chairman`.

### 6.3 Implementation notes for a rebuild
- All multi-model calls should be made **concurrently** with `httpx.AsyncClient` (not
  sequentially) — this is the entire point of using an ensemble without paying a serial
  latency tax.
- Every LLM call site must be wrapped to catch exceptions and timeouts and fall back
  gracefully — LLM Council outages must never crash the pipeline, only degrade scoring
  quality.

---

## 7. Agent 1 — Job Discovery (`agents/agent1_job_discovery.py`)

### 7.1 Data model
```python
@dataclass
class JobPosting:
    platform: str        # "naukri" | "indeed" | "linkedin"
    title: str
    company: str
    location: str
    url: str
    jd_text: str
    posted_date: str
    job_id: str
    tags: list[str]
    apply_url: str        # pre-resolved apply URL when discoverable cheaply
```

### 7.2 `JobDiscoveryAgent.discover()`
Orchestrates one scraper per enabled platform (per `config.search.platforms`), then
**deduplicates** results by the composite key `(platform, company.lower(), title.lower())`.

### 7.3 Per-platform scraping strategy (fallback chains)
- **Naukri** (`NaukriScraper`):
  1. Primary: Naukri's internal `/jobapi/v3/search` JSON API, called directly with
     `appid=109` / `systemid=109` headers (mimics the official web client; no login
     required for search).
  2. Fallback: ScraperAPI-rendered HTML fetch (handles Akamai bot-protection blocking).
  3. Further fallback: `NaukriBrowserScraper` — a full Playwright browser session reusing
     a persistent profile directory, for when even ScraperAPI is blocked.
- **Indeed** (`IndeedScraper`):
  1. Primary: `in.indeed.com/rss` feed (structured, no bot-protection issues).
  2. Fallback: ScraperAPI HTML render + BeautifulSoup parse.
- **LinkedIn** (`LinkedInScraper`):
  1. Primary: direct unauthenticated fetch of LinkedIn's public "guest" jobs API
     (`linkedin.com/jobs-guest/...`) — no login needed for search/listing.
  2. Fallback: ScraperAPI render.

### 7.4 Shared helpers
- `scraperapi_fetch(url, render: bool, ...)` — calls ScraperAPI with `country_code=in`,
  retries up to 3 times with exponential backoff (`wait = 10 * (attempt + 1)` seconds).
- `_debug_dump_html(...)` — writes raw HTML responses to `logs/debug/` for offline
  troubleshooting when scraping breaks (selectors/APIs change frequently on these sites).

### 7.5 Rebuild notes
- Treat every scraper as **3-tier**: free direct API/feed → paid rendering proxy → full
  browser automation. Never depend on only one tier — job boards change bot-defenses
  often.
- ScraperAPI (or an equivalent rendering proxy) is a paid, credit-metered service; design
  the retry budget conservatively (3 attempts) to avoid burning credits on a broken
  selector.

---

## 8. Council Job-Scoring Filter (`utils/job_scorer.py`)

Runs **after** discovery and **before** tailoring, to avoid wasting resume-generation and
application effort on poor-fit jobs.

### 8.1 Flow
```python
score, reasoning = score_job(job: JobPosting, profile: dict) -> tuple[float, str]
```
1. Builds an applicant-context string (name, location, ~years experience, seniority
   target, key skills) via `_build_applicant_context(profile)`.
2. Builds a scoring prompt (`_build_score_prompt`) embedding the job title/company/
   location/platform and the first 800 chars of the JD text.
3. Calls `ask_council_brief()` with a system prompt defining a strict 0–10 rubric:
   - 0–3 poor fit, 4–5 marginal, 6–7 good, 8–9 excellent, 10 perfect — and a required
     response format:
     ```
     SCORE: <number 0-10>
     REASONING: <2-3 sentences>
     ```
4. `_parse_score_response()` extracts `SCORE:` via regex (defaults to 5.0 if unparsable,
   clamped to `[0, 10]`) and `REASONING:` (truncated to 300 chars).
5. On any LLM error/unavailability, falls back to `_heuristic_score()`:
   - Base score 5.0.
   - +2.0 if title contains VP-level terms (`vp`, `vice president`, `svp`, `director`, etc).
   - +1.0 if title contains senior-but-not-VP terms (`lead`, `senior`, `manager`, `head`).
   - +2.0 if title matches target role type (`product owner`, `business analyst`,
     `product manager`, `product lead`, `product analyst`).
   - + up to 1.0 more from JD keyword hits (`agile`, `scrum`, `stakeholder`, `roadmap`,
     `product`, `analytics`, `digital`), weighted 0.3 each.
   - Clamped to `[0, 10]`.

### 8.2 `filter_jobs_by_score(jobs, profile, min_score=6.0, verbose=True)`
Scores every job, prints a `[+]`/`[-]` pass/fail line per job (with the reasoning excerpt
on failures), and returns only jobs meeting `min_score`. Scoring errors per-job are caught
individually (logged as `[?]`) and default to a neutral 5.0 rather than aborting the whole
batch.

This filter is gated by `config.council.use_for.job_scoring` and threshold
`config.council.min_job_score`; both `run_pipeline()` and `--dry-run` mode apply it
identically right after the discovery checkpoint.

---

## 9. Agent 2 — Resume Tailoring (`agents/agent2_resume_tailoring.py`)

### 9.1 Keyword extraction
`extract_keywords(jd_text, top_n=25)` — tokenizes the job description, applies a 3×
weight boost to a curated domain-keyword list (product/BA/agile vocabulary), and ranks by
weighted frequency to produce the top N keywords for a given JD.

### 9.2 `ResumeTailoringAgent.tailor(job: JobPosting) -> Path | None`
1. Loads the base `.docx` resume.
2. `_update_summary()` — tries `_generate_council_summary()` first (LLM Council rewrites
   the professional summary section to emphasize JD-relevant language); if the council is
   unavailable, falls back to prepending extracted keywords to the existing summary
   paragraph.
3. `_update_skills()` — appends any JD keywords from `extract_keywords()` that are missing
   from the existing Skills section.
4. `_find_section(doc, heading_name)` — locates resume sections either by exact heading
   style match or by a bold/ALL-CAPS paragraph heuristic (handles resumes that don't use
   Word's built-in heading styles).
5. `_output_path(job)` — builds the output filename as
   `resume_{company}_{role}_{date}.docx` and saves into `resume/tailored/` (gitignored).

Gated by `config.council.use_for.resume_tailoring`.

---

## 10. AI Form Filler (`utils/ai_form_filler.py`)

The general-purpose engine for answering arbitrary application-form fields encountered
during browser automation in Agent 3. Three-tier strategy, cheapest/most-deterministic
first:

### 10.1 Tier 1 — Honeypot detection
`_is_honeypot(el, label)` — regex on label/name text plus CSS-hidden checks
(`display:none`, `visibility:hidden`, off-screen positioning like `left:-9999px`).
Honeypot fields are skipped entirely (never filled) — they exist on real ATS forms purely
to trap bots.

### 10.2 Tier 2 — Preset pattern matching
`_PRESET_PATTERNS` — an ordered list of `(regex, value_or_callable)` pairs covering the
most common screening fields: name, email, phone, work authorization, sponsorship needs,
years of experience, current/expected CTC, notice period, relocation willingness, EEO
fields (defaulting to non-disclosure answers). Notably, work-authorization patterns are
deliberately scoped to avoid blanket-matching generic "citizen" phrasing, since incorrectly
answering US-citizenship-specific questions for a India-based applicant would produce
wrong answers — pattern specificity here matters more than coverage.

`_RADIO_PRESET`, `_select_radio()`, `_radio_group_question()` apply the same preset logic
to radio-button groups, handling both the `<fieldset><legend>` pattern and the flat
name-grouped `<input type=radio name=X>` pattern.

### 10.3 Tier 3 — LLM Council fallback
For fields that match neither a honeypot nor any preset pattern, `_council_answer()` /
`_single_model_answer()` ask the LLM Council (or single available backend) to answer using
the applicant's `resume_summary` and profile as context, then `_sanitize_value()` strips
the response down to a single clean line suitable for a form field.

**Hard cap**: `_MAX_COUNCIL_CALLS_PER_PASS = 4` — at most 4 LLM calls per form-fill pass,
to bound latency and avoid ATS session timeouts on long multi-step wizards. Once the cap
is hit, any remaining unmatched fields are left blank/skipped rather than blocking.

### 10.4 Browser interaction helpers
- `_get_label(page, el)` — 6-tier fallback label resolution: `aria-label` →
  `aria-labelledby` → `<label for=...>` → closest ancestor `<label>` → `placeholder` →
  `name` attribute. Necessary because ATS forms are wildly inconsistent in how labels are
  associated with inputs.
- `_react_fill(page, el, value)` — fills React-controlled inputs by invoking the **native**
  `HTMLInputElement`/`HTMLTextAreaElement` prototype value setter (bypassing React's
  shadowed setter) and then dispatching synthetic `input`, `change`, and `blur` events.
  Plain Playwright `.fill()` is insufficient because React's controlled-component state
  ignores DOM mutations that don't go through its tracked event handlers.

### 10.5 Public API
`fill_page_fields(page, ...)`, `fill_radio_groups(page, ...)`, `get_preset_answer(label)`,
`find_best_option(options, target)` — consumed by Agent 3's per-platform handlers.

Gated by `config.council.use_for.form_filling`.

---

## 11. Agent 3 — Application Submission (`agents/agent3_application.py`)

The largest and most complex subsystem — full browser automation against each platform's
real submission flow (or, for direct-apply platforms, the external ATS the job redirects
to).

### 11.1 Routing: in-platform vs. direct-URL
```python
_DIRECT_URL_PLATFORMS = {"naukri", "indeed", "linkedin"}
```
For these three, Agent 3 does **not** log into the platform and apply in-platform by
default — instead it follows the job's URL/apply-URL out to wherever it actually leads
(which is very often an external ATS like Workday or Greenhouse). `_process_platform()`
branches:
- Direct-URL platforms → `_apply_via_job_url()`.
- Everything else → log in to the platform first, then `_apply_to_job()`.

### 11.2 Per-platform / per-ATS handlers
- `BaseApplicationHandler` — shared interface/utilities.
- `NaukriHandler`, `IndeedHandler`, `LinkedInHandler` — platform-native apply flows
  (e.g. LinkedIn "Easy Apply" multi-step modal).
- `WorkdayHandler` — by far the most elaborate, because Workday's UI is the most
  adversarial to automate (see §11.3).
- `GreenhouseHandler`, `LeverHandler`, `GenericAtsHandler` — simpler ATS forms with more
  standard DOM structure.
- ATS detection: `detect_ats(url)` — regex-matches the apply URL's domain against known
  ATS hostnames: Workday, Greenhouse, Lever, iCIMS, Taleo, SmartRecruiters,
  SuccessFactors, BambooHR, Jobvite, Ashby.

### 11.3 Workday — the 5-tier "robust click" engine
Workday pages are built with web components and frequently have invisible click-catching
overlays, so a single click strategy is not reliable. The handler tries, **in order**,
until one succeeds:
1. Normal Playwright `.click()`.
2. Focus the element + send a trusted `Enter` keypress (works even when an invisible
   overlay would block a mouse click).
3. A **trusted-coordinate click** — dispatch a real OS-level mouse click at the element's
   bounding-box coordinates, which can pierce certain overlay-based click-jacking
   protections that synthetic JS clicks cannot.
4. A JS-dispatched `.click()` call directly on the DOM node.
5. Playwright's `force: true` click (bypasses actionability checks entirely) as the last
   resort.

Additional Workday-specific handling:
- **Popup-window logins**: some Workday tenants open the sign-in form in a separate
  popup window rather than inline; the handler must listen for new-page/popup events and
  switch context to interact with it.
- **Sign-In vs. Create-Account disambiguation**: the account step on many Workday tenants
  shows both a "Sign In" and a "Create Account" form on the same page; the handler must
  identify and use the correct one to avoid accidentally creating a duplicate Workday
  account.
- **Cookie-banner dismissal** before any interaction, since banners can sit on top of and
  block subsequent clicks.
- **Honeypot field avoidance** reuses `ai_form_filler._is_honeypot` during wizard steps.
- **Multi-step wizard navigation**: up to ~18 sequential steps per application, advancing
  via "Next"/"Continue" buttons located through the same robust-click engine.

### 11.4 Control-flow exceptions
- `SkipApplication` — raised to abandon a single job cleanly (e.g. already applied today,
  unsupported flow) without aborting the run.
- `ExternalApplicationRequired` — raised when the apply flow redirects somewhere the
  handler can't automate (e.g. a phone-only application), surfaced to the operator rather
  than silently dropped.

### 11.5 Application log (`ApplicationLog`)
- Persists to `logs/applications.json`.
- Keyed by `"{platform}::{company}::{title}"`.
- Deduplicates by **same-day submission** — re-running the pipeline the same day will not
  re-apply to a job already logged as submitted that day.
- `todays_summary()` returns a 3-tuple: `(submitted, failed, skipped)` lists, used to build
  the daily Telegram report.

### 11.6 `ApplicationAgent` orchestration
- `run(jobs, tailored_resumes)` — main loop over all filtered/tailored jobs.
- `_process_platform()` — routes per §11.1.
- `_handle_captcha()` — on CAPTCHA detection, sends a Telegram alert and polls for an
  operator `/captcha_done` reply, honoring a configurable timeout before giving up on that
  job.
- `_get_naukri_session()` / `_get_linkedin_handler()` — lazily authenticate once per run
  and cache the session/handler, rather than logging in per-job.
- `_apply_naukri_via_render()` — 4-tier apply-URL resolution priority: (1) `apply_url`
  already resolved by Agent 1 during discovery, (2) Naukri's free JSON API, (3) Naukri's
  authenticated API (using the session from `_get_naukri_session()`), (4) ScraperAPI
  render as last resort.
- `_handle_external_apply()` — when a direct-URL platform job redirects to an external
  ATS, runs `detect_ats()` and dispatches to the matching handler, supplying credentials
  either from `credentials.enc` or freshly collected via Telegram if the ATS account
  doesn't yet have stored credentials.

### 11.7 Notifications
- `TelegramNotifier` — sends formatted Markdown messages (daily summaries, CAPTCHA
  alerts, credential requests) via the Telegram Bot API `sendMessage` endpoint, and polls
  `getUpdates` for operator replies (`/captcha_done`, `/creds user:pass`, `/skip`)
  with configurable timeouts.
- `EmailNotifier` — alternate/additional notification channel (used where Telegram isn't
  configured).

### 11.8 Rebuild notes
- Don't underestimate Workday: budget the most implementation time here. Real Workday
  tenants vary significantly (popup vs inline login, 1-step vs 18-step wizards, varying
  honeypot placement) — the 5-tier click fallback and account-step disambiguation are not
  optional polish, they are required for baseline functionality.
- Anti-automation countermeasures to replicate: `playwright-stealth`, a **persistent**
  browser profile directory (reuse cookies/session across runs to avoid repeated
  logins/CAPTCHAs — set via `BROWSER_PROFILE_DIR`), and custom Chromium launch args
  (notably `--disable-blink-features=AutomationControlled`).

---

## 12. Testing & Mock Harnesses

Rather than testing against live job sites/ATSs (fragile, rate-limited, and would spam
real employers with test applications), the project uses local mock servers:

- `mock_run.py` — a self-contained local HTTP server reproducing 9 named real-world bug
  scenarios discovered during development (e.g. "Indeed apply redirects to company site",
  "Indeed ATS URL embedded in raw HTML", "LinkedIn Easy Apply", "LinkedIn shows an
  already-Applied badge", "LinkedIn external popup apply", "Naukri via ScraperAPI",
  "Workday login-wall", "generic BambooHR form"). Exists so regressions in any of these
  specific flows are caught automatically rather than rediscovered in production.
- `test_citi_replica.py` — a high-fidelity replica of a specific real employer's Workday
  instance (cookie banner, invisible click-catcher overlay, honeypot field, dual
  Sign-In/Create-Account forms, multi-step wizard) used as the primary Workday regression
  test.
- `test_workday_mock.py`, `test_workday_popup.py`, `test_workday_account_step.py` —
  focused unit/integration tests for individual Workday sub-behaviors.
- `test_pipeline.py` — end-to-end test of the full pipeline with scraping bypassed (feeds
  in canned `JobPosting` objects).
- `test_one_job.py` — applies to a single job, bypassing discovery/scoring/tailoring, for
  fast iteration on Agent 3 logic alone.
- `create_test_resume.py` — generates a realistic dummy `.docx` resume for use across the
  other tests without needing the operator's real resume.

**Rebuild guidance**: build these mocks early, in parallel with each handler — they are
the only practical way to iterate on ATS automation logic without rate-limiting or
flagging real accounts on real job boards.

---

## 13. Orchestration & CLI (`orchestrator.py`)

```
python orchestrator.py                       # full pipeline
python orchestrator.py --dry-run              # discover + score + tailor, no submission
python orchestrator.py --init-creds           # interactive credential wizard
python orchestrator.py --setup-creds-from-env # CI-style credential bootstrap
python orchestrator.py --test-telegram        # verify Telegram connectivity
```

`run_pipeline(config)`:
1. Load `CRED_KEY` from env (exit with a hint message if missing).
2. Auto-bootstrap `credentials.enc` from env vars if missing and a CI signal is present.
3. Agent 1 discover → checkpoint raw results to `logs/discovered_jobs.json`.
4. Council job-scoring filter (§8) — exits early if zero jobs pass.
5. Agent 2 tailor per surviving job → `tailored_resumes: dict[str, Path]` keyed by
   `"{platform}::{company}::{title}"`.
6. Agent 3 `ApplicationAgent.run(jobs, tailored_resumes)`.

`--dry-run` mode mirrors steps 1–5 (minus credential loading being optional) and sends a
Telegram summary of discovered jobs instead of submitting anything — explicitly useful for
validating discovery/scoring/tailoring changes without risking real applications.

---

## 14. Scheduling & Deployment

### 14.1 GitHub Actions (`.github/workflows/daily_apply.yml`)
- **Triggers**: `schedule` cron `30 3 * * *` (03:30 UTC = 09:00 IST) + `workflow_dispatch`
  for manual runs.
- Installs Python deps + Playwright browsers, then runs `python orchestrator.py` with all
  secrets (see §5.3) injected as env vars.
- Includes a failure-alert step: on workflow failure, posts a Telegram message (inline
  Python using `requests`) so the operator is notified even if the run itself crashed
  before reaching its own internal Telegram notification code.

### 14.2 Local Windows execution
- `first_time_setup.ps1` — one-time setup: persistent browser profile login, dependency
  install.
- `run_local.ps1` — loads all secrets from Windows Credential Manager, syncs the repo,
  installs deps, bootstraps `credentials.enc` if needed, sets `BROWSER_PROFILE_DIR` and
  `HEADLESS=true`, then runs `orchestrator.py` (or `--dry-run` based on a parameter).
- `setup_scheduler.ps1` — registers a Windows Task Scheduler job for a daily 9 AM local
  run, as an alternative/supplement to the GitHub Actions cron.

---

## 15. Build Order Recommendation (for an AI rebuilding this from scratch)

1. **Data model + config loading**: `JobPosting` dataclass, `config.yaml` /
   `applicant_answers.yaml` schemas, `load_config()`.
2. **Agent 4 (credentials)** — needed by everything downstream; build the Fernet
   encrypt/decrypt store and the two bootstrap flows first.
3. **Agent 1 (discovery)** — start with the cheapest tier per platform (free APIs/feeds)
   and get end-to-end discovery → dedup working before adding ScraperAPI/browser
   fallbacks.
4. **LLM Council** — build the backend-routing priority chain and the single-shot
   `ask_council_brief()` helper first (cheapest to integrate); add full 3-stage
   deliberation later for resume tailoring.
5. **Job scorer** — straightforward once the council helper exists; write the heuristic
   fallback in parallel so scoring never hard-depends on LLM availability.
6. **Resume tailoring** — keyword extraction is pure/deterministic and can be built and
   tested without any LLM; layer the council-based summary rewrite on top.
7. **AI form filler** — build tier 2 (presets) and tier 1 (honeypot) first since they're
   deterministic and testable without a browser; add tier 3 (LLM) last.
8. **Mock harnesses** (§12) — build alongside Agent 3, not after. Each new ATS handler
   should get a corresponding mock scenario before being considered done.
9. **Agent 3 (application)** — build platform handlers roughly in order of automation
   simplicity: Greenhouse/Lever/Generic ATS first (simple forms), then platform-native
   Naukri/Indeed/LinkedIn flows, then Workday last (most complex).
10. **Orchestrator + notifications + scheduling** — wire the full pipeline together last,
    once every subsystem works in isolation against its mocks.

---

## 16. Explicit Non-Goals / Things to Preserve As-Is

- Do not centralize all LLM calls behind a single blocking provider — the
  OpenRouter-free → Google-free → Anthropic-paid → heuristic fallback chain exists
  specifically so the system keeps working at zero cost.
- Do not remove the per-pass LLM call cap in form filling — uncapped LLM calls on long
  multi-step wizards risk ATS session timeouts.
- Do not store any credential, API key, or token in a tracked file. All secrets flow
  through environment variables, the encrypted credential store, or the OS credential
  manager only.
- Do not collapse the "direct-URL" platforms (Naukri/Indeed/LinkedIn) into the same
  code path as in-platform-login platforms — the routing distinction in §11.1 reflects a
  genuine behavioral difference in how those three platforms expose jobs versus how
  applications are actually submitted (frequently via an external ATS).
