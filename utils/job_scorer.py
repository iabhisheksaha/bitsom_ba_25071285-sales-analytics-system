"""
Council-powered job scoring.

Sends a job posting to the LLM Council for multi-perspective fit assessment.
Returns a score (0–10) and reasoning to filter out poor-fit jobs before applying.

Usage:
    from utils.job_scorer import score_job
    score, reasoning = score_job(job, applicant_profile)
    if score >= 6.0:
        # proceed with tailoring + application
"""

import re
from typing import Optional

from agents.agent1_job_discovery import JobPosting


# ---------------------------------------------------------------------------
# Applicant context builder
# ---------------------------------------------------------------------------

def _build_applicant_context(profile: dict) -> str:
    return (
        f"Name: {profile.get('name', 'Senior Product/BA professional')}\n"
        f"Location: {profile.get('location', 'Pune, Maharashtra, India')}\n"
        f"Experience: ~12 years in Product Management and Business Analysis\n"
        f"Seniority target: VP / Senior Vice President / Group VP level\n"
        f"Key skills: Product Strategy, Stakeholder Management, Agile/Scrum, "
        f"Data Analytics, Digital Transformation, Cross-functional Leadership"
    )


# ---------------------------------------------------------------------------
# Scoring prompt
# ---------------------------------------------------------------------------

_SCORE_SYSTEM = """You are a career advisor evaluating job fit for a senior product/BA professional.
Score job-applicant fit on a scale of 0-10 where:
  0-3: Poor fit (wrong role type, drastically under/over-leveled, wrong domain)
  4-5: Marginal fit (some mismatch in seniority, location, or requirements)
  6-7: Good fit (role aligns well, minor gaps)
  8-9: Excellent fit (strong match across seniority, skills, and domain)
  10:  Perfect fit (tailor-made)

Respond in exactly this format:
SCORE: <number 0-10>
REASONING: <2-3 sentences>"""


def _build_score_prompt(job: JobPosting, applicant_context: str) -> str:
    jd_excerpt = (job.jd_text or "")[:800]
    return (
        f"Evaluate fit for this job posting:\n\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Platform: {job.platform}\n"
        f"Job description excerpt:\n{jd_excerpt}\n\n"
        f"Applicant profile:\n{applicant_context}\n\n"
        f"Score the fit (0-10) and explain briefly."
    )


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _parse_score_response(text: str) -> tuple[float, str]:
    """Extract (score, reasoning) from council answer."""
    score = 5.0  # default
    reasoning = text.strip()

    # Extract SCORE: N
    m = re.search(r"SCORE\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if m:
        try:
            score = float(m.group(1))
            score = max(0.0, min(10.0, score))
        except ValueError:
            pass

    # Extract REASONING:
    m2 = re.search(r"REASONING\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if m2:
        reasoning = m2.group(1).strip()[:300]

    return score, reasoning


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_job(job: JobPosting, profile: "dict | None" = None) -> tuple[float, str]:
    """
    Score a job posting for applicant fit using the LLM Council.

    Returns:
        (score: float 0-10, reasoning: str)
    """
    if profile is None:
        from utils.ai_form_filler import load_applicant_profile
        profile = load_applicant_profile()

    applicant_ctx = _build_applicant_context(profile)
    question = _build_score_prompt(job, applicant_ctx)

    try:
        from utils.llm_council import ask_council_brief
        raw = ask_council_brief(question, system=_SCORE_SYSTEM, max_tokens=128)
        if raw:
            return _parse_score_response(raw)
    except ImportError:
        pass
    except Exception as exc:
        print(f"  [JobScorer] Council error: {exc}")

    # Heuristic fallback when no LLM available
    return _heuristic_score(job, profile)


def _heuristic_score(job: JobPosting, profile: dict) -> tuple[float, str]:
    """Simple keyword-based scoring when no LLM is available."""
    score = 5.0
    reasons = []
    title_lower = job.title.lower()
    jd_lower = (job.jd_text or "").lower()

    # Seniority check
    vp_terms = ("vp", "vice president", "svp", "senior vice", "group vice", "avp",
                 "assistant vice president", "director")
    if any(t in title_lower for t in vp_terms):
        score += 2.0
        reasons.append("VP-level title aligns with seniority target.")
    elif any(t in title_lower for t in ("lead", "senior", "manager", "head")):
        score += 1.0
        reasons.append("Senior-level title partially aligns.")

    # Role type check
    po_ba_terms = ("product owner", "business analyst", "product manager", "ba",
                   "product lead", "product analyst")
    if any(t in title_lower for t in po_ba_terms):
        score += 2.0
        reasons.append("Role type matches Product Owner / Business Analyst target.")

    # JD keyword match
    good_kw = {"agile", "scrum", "stakeholder", "roadmap", "product", "analytics", "digital"}
    jd_hits = sum(1 for kw in good_kw if kw in jd_lower)
    score += min(jd_hits * 0.3, 1.0)

    score = max(0.0, min(10.0, score))
    return score, " ".join(reasons) or "Heuristic score — LLM Council not available."


def filter_jobs_by_score(jobs: "list[JobPosting]", profile: "dict | None" = None,
                         min_score: float = 6.0,
                         verbose: bool = True) -> "list[JobPosting]":
    """
    Score all jobs and return only those meeting min_score.
    Logs scores to stdout if verbose=True.
    """
    if not jobs:
        return jobs

    scored: "list[tuple[float, str, JobPosting]]" = []
    for job in jobs:
        try:
            s, reason = score_job(job, profile)
            scored.append((s, reason, job))
            if verbose:
                icon = "+" if s >= min_score else "-"
                print(f"  [{icon}] {s:.1f}/10  {job.company} — {job.title}")
                if s < min_score:
                    print(f"        Reason: {reason[:80]}")
        except Exception as exc:
            if verbose:
                print(f"  [?] score_job error for {job.company}: {exc}")
            scored.append((5.0, "scoring error", job))

    passing = [job for (s, _, job) in scored if s >= min_score]
    if verbose:
        print(f"\n  [JobScorer] {len(passing)}/{len(jobs)} jobs passed score >= {min_score}")
    return passing
