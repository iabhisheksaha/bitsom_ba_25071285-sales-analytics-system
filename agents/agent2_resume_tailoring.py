"""
Agent 2: Resume Tailoring
Reads a base .docx resume, extracts keywords from the target JD,
and produces a tailored copy that surfaces matching skills and experience.
"""

import re
import copy
from datetime import date
from pathlib import Path
from typing import Optional

from docx import Document
from docx.oxml.ns import qn

from agents.agent1_job_discovery import JobPosting


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

# High-value terms commonly required for VP-level BA / PO roles
_DOMAIN_KEYWORDS = {
    # Product / BA competencies
    "product roadmap", "product strategy", "user story", "agile", "scrum", "kanban",
    "backlog", "sprint", "mvp", "stakeholder management", "business requirements",
    "gap analysis", "process improvement", "brd", "frd", "prd", "go-to-market",
    "product lifecycle", "ux", "ui", "design thinking", "kpi", "okr",
    # Technical / data
    "sql", "data analysis", "analytics", "tableau", "power bi", "api", "erp", "crm",
    "jira", "confluence", "azure devops", "figma",
    # Leadership / seniority
    "cross-functional", "p&l", "budget", "vendor management", "governance",
    "digital transformation", "change management", "executive", "leadership",
}

_STOP_WORDS = {
    "and", "the", "for", "with", "you", "are", "will", "have", "has", "been",
    "that", "this", "from", "our", "your", "their", "they", "must", "should",
    "able", "not", "can", "all", "any", "its", "also", "into", "more", "over",
    "than", "but", "was", "were", "one", "two", "or", "of", "to", "in", "at",
    "be", "an", "a", "is", "it", "as", "by", "on", "we",
}


def extract_keywords(jd_text: str, top_n: int = 25) -> list[str]:
    """
    Return a ranked list of the most relevant keywords from the JD.
    Domain terms are boosted; generic stop words are excluded.
    """
    text_lower = jd_text.lower()

    # Score domain keywords by presence
    domain_hits: list[tuple[str, int]] = []
    for kw in _DOMAIN_KEYWORDS:
        count = len(re.findall(r'\b' + re.escape(kw) + r'\b', text_lower))
        if count > 0:
            domain_hits.append((kw, count * 3))  # 3× weight

    # Score individual tokens by frequency
    tokens = re.findall(r'\b[a-z][a-z+#.-]{2,}\b', text_lower)
    freq: dict[str, int] = {}
    for tok in tokens:
        if tok not in _STOP_WORDS:
            freq[tok] = freq.get(tok, 0) + 1

    token_hits = [(w, c) for w, c in freq.items() if c >= 2]

    combined: dict[str, int] = {}
    for kw, score in domain_hits:
        combined[kw] = score
    for kw, score in token_hits:
        combined.setdefault(kw, 0)
        combined[kw] += score

    ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)
    return [kw for kw, _ in ranked[:top_n]]


# ---------------------------------------------------------------------------
# Resume paragraph utilities
# ---------------------------------------------------------------------------

def _paragraph_text(para) -> str:
    return para.text.strip()


def _find_section_indices(doc: Document, section_names: list[str]) -> dict[str, int]:
    """Return {section_name: paragraph_index} for known section headers."""
    mapping: dict[str, int] = {}
    for i, para in enumerate(doc.paragraphs):
        txt = para.text.strip().lower()
        for name in section_names:
            if name in txt and para.style.name.lower().startswith("heading"):
                mapping[name] = i
                break
    return mapping


def _replace_run_text(run, new_text: str) -> None:
    """Update text in a run while preserving its existing XML formatting."""
    run.text = new_text


# ---------------------------------------------------------------------------
# Core tailoring logic
# ---------------------------------------------------------------------------

class ResumeTailoringAgent:

    SKILL_SECTION_NAMES = ["skills", "technical skills", "core competencies", "key skills", "domain expertise"]
    SUMMARY_SECTION_NAMES = ["summary", "profile", "objective", "about", "professional summary"]

    def __init__(self, config: dict):
        self.base_path = Path(config.get("resume", {}).get("base_path", "resume/base_resume.docx"))
        self.output_dir = Path(config.get("resume", {}).get("output_dir", "resume/tailored"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def tailor(self, job: JobPosting) -> Optional[Path]:
        """
        Produce a tailored resume for the given job.  Returns the output path,
        or None if the base resume cannot be found.
        """
        if not self.base_path.exists():
            print(f"[Agent2] Base resume not found at {self.base_path}. Skipping.")
            return None

        keywords = extract_keywords(job.jd_text)
        print(f"[Agent2] Top keywords for {job.company}: {keywords[:10]}")

        doc = Document(str(self.base_path))
        doc = self._update_summary(doc, job, keywords)
        doc = self._update_skills(doc, keywords)

        output_path = self._output_path(job)
        doc.save(str(output_path))
        print(f"[Agent2] Tailored resume saved → {output_path}")
        return output_path

    # ------------------------------------------------------------------
    # Section mutators
    # ------------------------------------------------------------------

    def _update_summary(self, doc: Document, job: JobPosting, keywords: list[str]) -> Document:
        """Prepend a targeted summary sentence if a Summary section exists."""
        section_idx = self._find_section(doc, self.SUMMARY_SECTION_NAMES)
        if section_idx is None:
            return doc

        # The paragraph immediately after the heading is the summary body
        body_idx = section_idx + 1
        if body_idx >= len(doc.paragraphs):
            return doc

        para = doc.paragraphs[body_idx]
        existing = para.text.strip()

        # Build a role-aligned opener
        role_phrase = job.title if job.title else "VP-level"
        top_kw = ", ".join(k.title() for k in keywords[:5])
        opener = (
            f"Senior product and business analysis leader with expertise in {top_kw}, "
            f"seeking {role_phrase} opportunities. "
        )

        if para.runs:
            # Prepend to first run so formatting is preserved
            para.runs[0].text = opener + para.runs[0].text
        else:
            para.add_run(opener + existing)

        return doc

    def _update_skills(self, doc: Document, keywords: list[str]) -> Document:
        """
        Merge JD keywords into the Skills section.
        Appends missing keywords to the last run of the skills paragraph.
        """
        section_idx = self._find_section(doc, self.SKILL_SECTION_NAMES)
        if section_idx is None:
            return doc

        body_idx = section_idx + 1
        if body_idx >= len(doc.paragraphs):
            return doc

        para = doc.paragraphs[body_idx]
        existing_text = para.text.lower()

        missing = [kw.title() for kw in keywords if kw.lower() not in existing_text]
        if not missing:
            return doc

        addition = " | " + " | ".join(missing[:10])
        if para.runs:
            para.runs[-1].text += addition
        else:
            para.add_run(addition)

        return doc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_section(self, doc: Document, candidates: list[str]) -> Optional[int]:
        for i, para in enumerate(doc.paragraphs):
            txt = para.text.strip().lower()
            if not any(c in txt for c in candidates):
                continue
            # Match formal Heading styles OR bold/ALL-CAPS Normal paragraphs
            style = para.style.name.lower()
            is_heading = style.startswith("heading")
            is_bold_label = (
                para.text.strip().isupper() or
                (para.runs and all(r.bold for r in para.runs if r.text.strip()))
            )
            if is_heading or is_bold_label:
                return i
        return None

    def _output_path(self, job: JobPosting) -> Path:
        safe_company = re.sub(r"[^a-zA-Z0-9_-]", "_", job.company)[:30]
        safe_role = re.sub(r"[^a-zA-Z0-9_-]", "_", job.title)[:20]
        filename = f"resume_{safe_company}_{safe_role}_{date.today().isoformat()}.docx"
        return self.output_dir / filename
