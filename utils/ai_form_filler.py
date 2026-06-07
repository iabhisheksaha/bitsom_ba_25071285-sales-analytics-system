"""
AI-powered form filling for ATS pages (Workday, Greenhouse, Lever, etc.).

Three-tier answer strategy:
  1. Honeypot detection  — skip bot-trap fields silently
  2. Preset patterns     — fast lookup for common questions (work auth, sponsorship…)
  3. LLM Council         — multi-model deliberation for complex/unknown questions

Public API:
    fill_page_fields(page, profile, resume_text)  -> int  (fields filled)
    fill_radio_groups(page, profile, resume_text) -> int  (groups filled)
    load_applicant_profile(config_path)           -> dict
    extract_resume_text(resume_path)              -> str
"""

import os
import re
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Applicant profile loader
# ---------------------------------------------------------------------------

_PROFILE_CACHE: "dict | None" = None


def load_applicant_profile(config_path: str = "config/config.yaml") -> dict:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is not None:
        return _PROFILE_CACHE
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        raw = cfg.get("applicant", {})
        name = raw.get("name", "")
        parts = name.split(" ", 1)
        _PROFILE_CACHE = {
            "name":         name,
            "first_name":   parts[0] if parts else "",
            "last_name":    parts[1] if len(parts) > 1 else "",
            "email":        raw.get("email", ""),
            "phone":        raw.get("phone", ""),
            "linkedin_url": raw.get("linkedin_url", ""),
            "location":     raw.get("location", ""),
            "city":         raw.get("location", "").split(",")[0].strip(),
        }
    except Exception:
        _PROFILE_CACHE = {}
    return _PROFILE_CACHE


def extract_resume_text(resume_path: Path) -> str:
    """Return plain text from a .docx resume for AI context."""
    try:
        from docx import Document
        doc = Document(str(resume_path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Honeypot detection
# ---------------------------------------------------------------------------

_HONEYPOT_LABEL_RE = re.compile(
    r"robots?\s+only|do\s+not\s+(enter|fill)|if\s+you('re|\s+are)\s+human|"
    r"leave\s+(this|it)\s+blank|not\s+for\s+humans",
    re.IGNORECASE,
)
_HONEYPOT_NAME_RE = re.compile(
    r"beecatcher|honeypot|bot.?trap|spam.?check|h_?field",
    re.IGNORECASE,
)


def _is_honeypot(el, label: str) -> bool:
    if _HONEYPOT_LABEL_RE.search(label):
        return True
    try:
        name = el.get_attribute("name") or ""
        if _HONEYPOT_NAME_RE.search(name):
            return True
        style = el.get_attribute("style") or ""
        if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0", style, re.I):
            return True
        pos_left = re.search(r"left\s*:\s*-\d{3,}", style)
        if pos_left:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Preset patterns (fast lookup — no API call needed)
# ---------------------------------------------------------------------------

# (regex, answer_or_callable)
_PRESET_PATTERNS: "list[tuple[re.Pattern, object]]" = [
    # Personal info — from profile
    (re.compile(r"\bfirst\s*name\b", re.I),    lambda p, _: p.get("first_name", "")),
    (re.compile(r"\blast\s*name\b|\bsurname\b", re.I), lambda p, _: p.get("last_name", "")),
    (re.compile(r"\bfull\s*name\b|\bname\b",   re.I),  lambda p, _: p.get("name", "")),
    (re.compile(r"\be.?mail\b",                re.I),  lambda p, _: p.get("email", "")),
    (re.compile(r"\bphone|mobile|telephone\b", re.I),  lambda p, _: p.get("phone", "")),
    (re.compile(r"\blinkedin\b",               re.I),  lambda p, _: p.get("linkedin_url", "")),
    (re.compile(r"\bcity|current\s*location\b",re.I),  lambda p, _: p.get("city", "")),
    (re.compile(r"\blocation\b",               re.I),  lambda p, _: p.get("location", "")),

    # Work authorization / legal eligibility
    (re.compile(
        r"work.{0,20}authoriz|authoriz.{0,20}work|right.{0,12}work|"
        r"legally.{0,20}(eligible|authoriz)|eligible.{0,12}work|"
        r"permitted.{0,12}work|citizen|permanent\s+resident",
        re.I), "Yes"),

    # Sponsorship
    (re.compile(r"sponsor(ship)?|require.{0,15}sponsor|visa\s+sponsor", re.I), "No"),

    # Previously employed / family member of employee
    (re.compile(
        r"ever\s+been\s+employed|previously.{0,12}employ|former.{0,12}employ|"
        r"currently\s+employed\s+by|employed\s+by\s+(citi|the\s+company)|"
        r"relative|related\s+to.{0,20}employee|family\s+member.{0,20}employ",
        re.I), "No"),

    # Criminal / background
    (re.compile(r"criminal|felony|convicted|arrest", re.I), "No"),

    # Experience years
    (re.compile(r"total.{0,8}years?.{0,12}exp|years?.{0,12}total.{0,12}exp", re.I), "12"),
    (re.compile(r"years?.{0,12}(product.{0,12}(management|manager|owner))", re.I), "10"),
    (re.compile(r"years?.{0,12}(business.?analys|ba\s+experience)", re.I), "12"),
    (re.compile(r"years?\s+of\s+experience|experience.{0,8}years?", re.I), "12"),

    # Salary
    (re.compile(r"current.{0,12}(ctc|salary|compensation|package)", re.I), "20 LPA"),
    (re.compile(r"expect.{0,12}(ctc|salary|compensation)|salary.{0,12}expect", re.I), "30 LPA"),

    # Notice period
    (re.compile(r"notice.{0,12}period|serving.{0,6}notice|available.{0,8}(join|start)", re.I), "30 days"),

    # Relocation / travel
    (re.compile(r"\brelocat\b", re.I), "Yes"),
    (re.compile(r"travel.{0,12}(willing|required|percent)", re.I), "Yes, up to 25%"),

    # DEI / voluntary disclosures
    (re.compile(r"\bveteran\b|military.{0,8}service", re.I), "I am not a protected veteran"),
    (re.compile(r"\bdisabilit\b", re.I), "I choose not to self-identify"),
    (re.compile(r"^gender$|gender.{0,6}(identif|pronoun)", re.I), "Male"),
    (re.compile(r"ethnicity|race|national.{0,6}origin", re.I), "Asian"),
]


def _preset_answer(label: str, profile: dict, resume_text: str = "") -> Optional[str]:
    for pattern, answer in _PRESET_PATTERNS:
        if pattern.search(label):
            val = answer(profile, resume_text) if callable(answer) else answer
            return val or None
    return None


# ---------------------------------------------------------------------------
# Label extraction
# ---------------------------------------------------------------------------

def _get_label(page, el) -> str:
    """Best-effort label extraction for a form element."""
    # 1. aria-label
    try:
        v = el.get_attribute("aria-label") or ""
        if v and len(v) < 140:
            return v.strip().rstrip("*").strip()
    except Exception:
        pass

    # 2. aria-labelledby
    try:
        ref_id = el.get_attribute("aria-labelledby") or ""
        if ref_id:
            ref = page.query_selector(f"#{ref_id.split()[0]}")
            if ref:
                t = ref.inner_text().strip().rstrip("*").strip()
                if t:
                    return t
    except Exception:
        pass

    # 3. <label for="id">
    try:
        el_id = el.get_attribute("id") or ""
        if el_id:
            lbl = page.query_selector(f'label[for="{el_id}"]')
            if lbl:
                t = lbl.inner_text().strip().rstrip("*").strip()
                if t:
                    return t
    except Exception:
        pass

    # 4. Closest label ancestor
    try:
        t = el.evaluate("""el => {
            const lbl = el.closest('label');
            if (lbl) return lbl.innerText.trim();
            const wrap = el.closest('[data-automation-id], .form-field, .field-wrapper');
            if (wrap) {
                const l = wrap.querySelector('label');
                if (l) return l.innerText.trim();
            }
            return '';
        }""")
        if t and len(t) < 140:
            return t.rstrip("*").strip()
    except Exception:
        pass

    # 5. placeholder
    try:
        ph = el.get_attribute("placeholder") or ""
        if ph and len(ph) < 80:
            return ph.strip()
    except Exception:
        pass

    # 6. name attribute
    try:
        name = el.get_attribute("name") or ""
        if name:
            return re.sub(r"[_\-]", " ", name).strip()
    except Exception:
        pass

    return ""


# ---------------------------------------------------------------------------
# React-aware fill helper
# ---------------------------------------------------------------------------

def _react_fill(page, el, value: str) -> None:
    """Fill a React-controlled input: native setter + synthetic events."""
    page.evaluate("""([el, val]) => {
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set
            || Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype, 'value').set;
        if (setter) setter.call(el, val);
        el.dispatchEvent(new Event('input',  {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.dispatchEvent(new Event('blur',   {bubbles: true}));
    }""", [el, value])


# ---------------------------------------------------------------------------
# Council-powered AI fallback
# ---------------------------------------------------------------------------

def _council_answer(label: str, field_type: str,
                    profile: dict, resume_text: str) -> str:
    """Use the LLM Council (or single-model fallback) to answer an unknown field."""
    try:
        from utils.llm_council import ask_council_brief
    except ImportError:
        return _single_model_answer(label, field_type, profile, resume_text)

    system = (
        "You are filling a job application form. Reply with ONLY the value to "
        "enter in the field — no explanation, no quotes, no markdown."
    )
    question = (
        f"Job application field label: \"{label}\"\n"
        f"Field type: {field_type}\n\n"
        f"Applicant profile:\n"
        f"  Name: {profile.get('name', '')}\n"
        f"  Email: {profile.get('email', '')}\n"
        f"  Phone: {profile.get('phone', '')}\n"
        f"  Location: {profile.get('location', '')}\n"
        f"  LinkedIn: {profile.get('linkedin_url', '')}\n\n"
        f"Resume excerpt (first 600 chars):\n{resume_text[:600]}\n\n"
        f"What should be entered in this field?"
    )
    return ask_council_brief(question, system=system, max_tokens=64)


def _single_model_answer(label: str, field_type: str,
                         profile: dict, resume_text: str) -> str:
    """Single Claude Haiku call when council not available."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            f'Fill this job application field.\nLabel: "{label}"\nType: {field_type}\n'
            f"Profile: {profile}\nResume: {resume_text[:400]}\n"
            f"Reply with ONLY the value, nothing else."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Radio group handling
# ---------------------------------------------------------------------------

_RADIO_PRESET: "list[tuple[re.Pattern, str]]" = [
    (re.compile(r"work.{0,20}authoriz|right.{0,12}work|legally.{0,20}eligible|citizen", re.I), "Yes"),
    (re.compile(r"sponsor", re.I), "No"),
    (re.compile(r"ever\s+been\s+employed|previously.{0,12}employ|relative|related.*employee", re.I), "No"),
    (re.compile(r"criminal|felony|arrest", re.I), "No"),
    (re.compile(r"\brelocat\b", re.I), "Yes"),
    (re.compile(r"veteran", re.I), "I am not a protected veteran"),
    (re.compile(r"disabilit", re.I), "I choose not to self-identify"),
    (re.compile(r"^gender$|gender.*identif", re.I), "Male"),
]


def _select_radio(page, radios, target_value: str) -> bool:
    """Click the radio button whose label/value best matches target_value."""
    target = target_value.lower()
    for radio in radios:
        try:
            val = (radio.get_attribute("value") or "").lower()
            el_id = radio.get_attribute("id") or ""
            label_text = ""
            if el_id:
                lbl = page.query_selector(f'label[for="{el_id}"]')
                if lbl:
                    label_text = lbl.inner_text().lower()
            if val == target or label_text.startswith(target) or target in label_text:
                radio.click()
                page.wait_for_timeout(400)
                return True
        except Exception:
            pass
    # Fuzzy: first radio whose value contains first word of target
    first_word = target.split()[0] if target.split() else target
    for radio in radios:
        try:
            val = (radio.get_attribute("value") or "").lower()
            if first_word in val:
                radio.click()
                page.wait_for_timeout(400)
                return True
        except Exception:
            pass
    return False


def _radio_group_question(page, name: str, legend_text: str,
                          profile: dict, resume_text: str) -> Optional[str]:
    """Determine the answer for a radio group using presets or council."""
    combined = f"{legend_text} {name}".strip()
    for pattern, answer in _RADIO_PRESET:
        if pattern.search(combined):
            return answer
    # Council fallback for unknown radio groups
    try:
        from utils.llm_council import ask_council_brief
        system = (
            "You are filling a job application. For radio button questions, "
            "reply with ONLY the answer text — no explanation, no quotes."
        )
        q = (
            f'Radio button question: "{combined}"\n'
            f"Applicant: {profile.get('name', '')} in {profile.get('location', '')}\n"
            f"What should be selected? Reply with the answer text only."
        )
        return ask_council_brief(q, system=system, max_tokens=32)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fill_page_fields(page, profile: "dict | None" = None,
                     resume_text: str = "") -> int:
    """
    Fill all visible text/email/tel/number inputs on the page.
    Returns the count of fields filled.
    """
    if profile is None:
        profile = load_applicant_profile()

    filled = 0
    try:
        inputs = page.query_selector_all(
            "input[type='text'], input[type='email'], input[type='tel'], "
            "input[type='number'], input[type='url'], textarea, "
            "input:not([type])"
        )
        for inp in inputs:
            try:
                if not inp.is_visible():
                    continue
                if inp.get_attribute("disabled") or inp.get_attribute("readonly"):
                    continue
                # Skip already-filled fields
                try:
                    current = inp.input_value() or ""
                    if current.strip():
                        continue
                except Exception:
                    pass

                label = _get_label(page, inp)

                if _is_honeypot(inp, label):
                    print(f"    [FormFillerAI] Skipping honeypot field '{label[:50]}'")
                    continue

                value = _preset_answer(label, profile, resume_text)
                if not value and label:
                    field_type = inp.get_attribute("type") or "text"
                    value = _council_answer(label, field_type, profile, resume_text)

                if value:
                    _react_fill(page, inp, value)
                    print(f"    [FormFillerAI] '{label[:40]}' <- '{value[:40]}'")
                    filled += 1
            except Exception:
                pass
    except Exception:
        pass
    return filled


def fill_radio_groups(page, profile: "dict | None" = None,
                      resume_text: str = "") -> int:
    """
    Fill radio button groups on the current ATS page.
    Handles Workday's <fieldset>/<legend> pattern and name-grouped radios.
    Returns the count of groups filled.
    """
    if profile is None:
        profile = load_applicant_profile()

    filled = 0

    # Strategy 1: <fieldset><legend> pattern
    try:
        fieldsets = page.query_selector_all("fieldset")
        for fs in fieldsets:
            try:
                legend = fs.query_selector("legend")
                legend_text = legend.inner_text().strip() if legend else ""
                radios = fs.query_selector_all("input[type='radio']")
                if not radios:
                    continue
                answer = _radio_group_question(page, "", legend_text, profile, resume_text)
                if answer and _select_radio(page, radios, answer):
                    print(f"    [FormFillerAI] Radio '{legend_text[:40]}' <- '{answer[:30]}'")
                    filled += 1
            except Exception:
                pass
    except Exception:
        pass

    # Strategy 2: name-grouped radios (Workday's pattern)
    try:
        all_radios = page.query_selector_all("input[type='radio']")
        by_name: "dict[str, list]" = {}
        for r in all_radios:
            try:
                name = r.get_attribute("name") or ""
                if name:
                    by_name.setdefault(name, []).append(r)
            except Exception:
                pass

        for name, radios in by_name.items():
            try:
                # Check if we're inside a fieldset already handled
                in_fieldset = radios[0].evaluate("el => !!el.closest('fieldset')")
                if in_fieldset:
                    continue

                # Try aria-label on the group container
                group_label = name
                container = radios[0].evaluate_handle(
                    "el => el.closest('[role=\"group\"], [role=\"radiogroup\"]') || el.parentElement"
                ).as_element()
                if container:
                    aria = container.get_attribute("aria-label") or ""
                    if aria:
                        group_label = aria

                answer = _radio_group_question(page, name, group_label, profile, resume_text)
                if answer and _select_radio(page, radios, answer):
                    print(f"    [FormFillerAI] Radio '{group_label[:40]}' <- '{answer[:30]}'")
                    filled += 1
            except Exception:
                pass
    except Exception:
        pass

    return filled
