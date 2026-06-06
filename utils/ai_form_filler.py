"""
AI-powered form filling for job application ATS systems.

Approach sourced from:
- AIHawk: field-type routing, option fuzzy-matching (Levenshtein)
- srikar-kodakandla/linkedin-easyapply-using-AI: LinkedIn selectors, two-stage prompting
- ApplyPilot: profile.json pre-stored answer structure
- proficiently-claude-skills: Lever /apply trick, Greenhouse iframe extraction
"""

import os
import re
from pathlib import Path
from typing import Optional

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Preset patterns for common screening questions
# Checked first to avoid unnecessary API calls.
# ---------------------------------------------------------------------------

_PRESET_PATTERNS: list[tuple[str, str]] = [
    (r"work.{0,20}authoriz|authoriz.{0,20}work|right.{0,12}work|eligible.{0,12}work|"
     r"legally.{0,20}work|permit.{0,12}work|work.{0,12}permit", "Yes"),
    (r"sponsor", "No"),  # any sponsorship/visa-sponsorship question -> No (Indian citizen in India)
    (r"criminal|felony|convicted|arrest", "No"),
    (r"non.?compet|non.?disclosure|nda", "Yes"),
    (r"total.{0,8}years?.{0,12}exp|years?.{0,12}total.{0,12}exp", "12"),
    (r"years?.{0,12}product.{0,12}(management|manager|owner)", "10"),
    (r"years?.{0,12}(business.?analys|ba.?experience)", "12"),
    (r"years?.{0,12}experience|experience.{0,12}years?", "12"),
    (r"current.{0,12}(ctc|salary|compens|package)", "20 LPA"),
    (r"expect.{0,12}(ctc|salary|compens|package)|salary.{0,12}expect", "30 LPA"),
    (r"desired.{0,12}salary|salary.{0,12}desired", "30 LPA"),
    (r"notice.{0,12}period|serving.{0,6}notice|available.{0,8}(join|start)", "60 days"),
    (r"relocat", "Yes"),
    (r"travel.{0,12}(willing|required|percent|up to)", "Yes, up to 25%"),
    (r"remote.{0,12}work|work.{0,12}remote", "Hybrid"),
    (r"veteran|military.{0,8}service", "I am not a protected veteran"),
    (r"disabilit", "I choose not to self-identify"),
    (r"^gender$|gender.{0,6}(identif|pronoun)", "Male"),
    (r"ethnicity|race|national.{0,6}origin", "Asian"),
]


def load_applicant_profile(
    config_path: str = "config/config.yaml",
    answers_path: str = "config/applicant_answers.yaml",
) -> dict:
    """Load applicant details from config files into a flat dict."""
    import yaml

    profile: dict = {}

    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        raw = cfg.get("applicant", {})
        name = raw.get("name", "")
        parts = name.split(" ", 1)
        profile.update(
            {
                "name": name,
                "first_name": parts[0] if parts else "",
                "last_name": parts[1] if len(parts) > 1 else "",
                "email": raw.get("email", ""),
                "phone": raw.get("phone", ""),
                "linkedin_url": raw.get("linkedin_url", ""),
                "location": raw.get("location", "Pune, Maharashtra, India"),
            }
        )
    except Exception:
        pass

    try:
        with open(answers_path) as f:
            extra = yaml.safe_load(f) or {}
        profile.update(extra)
    except Exception:
        pass

    return profile


def extract_resume_text(resume_path: Path) -> str:
    """Return plain text from a .docx resume for use as AI context."""
    try:
        from docx import Document

        doc = Document(str(resume_path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Label extraction
# ---------------------------------------------------------------------------

def _get_element_label(page, element) -> str:
    """Extract the human-readable label for a form input element."""
    # 1. aria-label
    try:
        v = element.get_attribute("aria-label") or ""
        if v and len(v) < 120:
            return v.strip().rstrip("*").strip()
    except Exception:
        pass

    # 2. aria-labelledby → referenced element text
    try:
        ref_id = element.get_attribute("aria-labelledby") or ""
        if ref_id:
            ref = page.query_selector(f"#{ref_id.split()[0]}")
            if ref:
                txt = ref.inner_text().strip().rstrip("*").strip()
                if txt:
                    return txt
    except Exception:
        pass

    # 3. <label for="id">
    try:
        el_id = element.get_attribute("id") or ""
        if el_id:
            lbl = page.query_selector(f'label[for="{el_id}"]')
            if lbl:
                txt = lbl.inner_text().strip().rstrip("*").strip()
                if txt:
                    return txt
    except Exception:
        pass

    # 4. Closest label ancestor / container label
    try:
        txt = element.evaluate(
            """el => {
                const lbl = el.closest('label');
                if (lbl) return lbl.innerText;
                const wrap = el.closest('.artdeco-text-input--container, .fb-dash-form-element, '
                           + '[data-test-form-builder-text-input], .jobs-easy-apply-form-element');
                if (wrap) {
                    const l = wrap.querySelector('label');
                    if (l) return l.innerText;
                }
                return '';
            }"""
        )
        if txt and len(txt) < 120:
            return txt.strip().rstrip("*").strip()
    except Exception:
        pass

    # 5. placeholder
    try:
        v = element.get_attribute("placeholder") or ""
        if v and len(v) < 80:
            return v.strip()
    except Exception:
        pass

    # 6. name attribute (convert to words)
    try:
        name = element.get_attribute("name") or ""
        if name:
            return re.sub(r"[_\-]", " ", name).strip()
    except Exception:
        pass

    return ""


# ---------------------------------------------------------------------------
# Preset answer lookup
# ---------------------------------------------------------------------------

_PROFILE_FIELD_PATTERNS: list[tuple[str, str]] = [
    (r"first.?name", "first_name"),
    (r"last.?name|surname|family.?name", "last_name"),
    (r"^(full.?)?name$", "name"),
    (r"e.?mail|email.?address", "email"),
    (r"phone|mobile|telephone|cell", "phone"),
    (r"linkedin", "linkedin_url"),
    (r"city|current.{0,8}(city|location)|location", "city"),
    (r"state|province", "state"),
    (r"country", "country"),
    (r"zip|postal.?code", "zip_code"),
    (r"website|portfolio|personal.{0,8}url", "website_url"),
    (r"github", "github_url"),
    (r"university|school|college|institution", "university"),
    (r"graduation.{0,8}year|year.{0,8}graduate", "graduation_year"),
    (r"degree|highest.{0,8}(education|qualif)", "highest_education"),
    (r"current.{0,8}(role|title|position)|job.{0,8}title", "current_role"),
    (r"current.{0,8}(company|employer|organization)", "current_company"),
    (r"notice.{0,12}period|serving.{0,6}notice|available.{0,8}(join|start)", "notice_period"),
    (r"years?.{0,12}experience", "years_of_experience"),
    (r"expected.{0,12}(ctc|salary|compens)|salary.{0,12}expect|desired.{0,12}salary", "expected_ctc"),
    (r"current.{0,12}(ctc|salary|compens)", "current_ctc"),
]


def get_preset_answer(label: str, profile: dict) -> Optional[str]:
    """
    Return a pre-stored answer for a form field label, or None if not found.
    Checks profile field patterns first, then common screening patterns.
    """
    label_lower = label.lower().strip()

    # Profile field map
    for pattern, key in _PROFILE_FIELD_PATTERNS:
        if re.search(pattern, label_lower):
            value = profile.get(key, "")
            if value is not None and str(value).strip():
                return str(value).strip()

    # General screening patterns
    for pattern, answer in _PRESET_PATTERNS:
        if re.search(pattern, label_lower):
            return answer

    # Direct key match from applicant_answers.yaml custom entries
    for key, value in profile.items():
        if key.lower().replace("_", " ") == label_lower and value:
            return str(value)

    return None


# ---------------------------------------------------------------------------
# Claude API answer generation
# ---------------------------------------------------------------------------

def _build_system_prompt(profile: dict, resume_text: str) -> str:
    return (
        f"You are filling a job application form on behalf of {profile.get('name', 'the applicant')}.\n\n"
        f"Applicant Profile:\n"
        f"  Name:           {profile.get('name', '')}\n"
        f"  Email:          {profile.get('email', '')}\n"
        f"  Phone:          {profile.get('phone', '')}\n"
        f"  Location:       {profile.get('location', 'Pune, Maharashtra, India')}\n"
        f"  Current Role:   {profile.get('current_role', 'Senior Product Manager / Business Analyst')}\n"
        f"  Yrs Experience: {profile.get('years_of_experience', '12')}\n"
        f"  Current CTC:    {profile.get('current_ctc', '20 LPA')}\n"
        f"  Expected CTC:   {profile.get('expected_ctc', '30 LPA')}\n"
        f"  Notice Period:  {profile.get('notice_period', '60 days')}\n"
        f"  Work Auth:      Authorized to work in India, no sponsorship needed\n\n"
        + (("Resume Summary:\n" + profile.get("resume_summary", resume_text[:800]) + "\n\n")
           if (profile.get("resume_summary") or resume_text) else "")
        + "Rules:\n"
        "- Reply with ONLY the answer value. No explanation, no quotes unless part of the value.\n"
        "- For yes/no questions, reply Yes or No.\n"
        "- For numeric fields, reply with a number only.\n"
        "- For dropdown/radio, reply with the EXACT option text from the list provided.\n"
        "- Never fabricate credentials or certifications not in the profile."
    )


def ask_claude(
    label: str,
    field_type: str,
    options: list[str],
    profile: dict,
    resume_text: str = "",
) -> str:
    """Ask Claude to answer a form field. Falls back gracefully if API key not set."""
    if not ANTHROPIC_API_KEY:
        if options:
            return options[0]
        if field_type == "number":
            return "12"
        return ""

    try:
        import anthropic

        if options:
            user_msg = (
                f'Form field: "{label}"\n'
                f"Options: {options}\n"
                f"Choose the most appropriate option and reply with its EXACT text."
            )
        elif field_type == "number":
            user_msg = f'Form field: "{label}"\nReply with a single integer.'
        elif field_type in ("textarea", "text_long"):
            user_msg = (
                f'Form field: "{label}"\n'
                f"Write 2-3 professional sentences for the applicant."
            )
        else:
            user_msg = f'Form field: "{label}"\nReply with a brief appropriate value (one line).'

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=300,
            system=_build_system_prompt(profile, resume_text),
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text.strip()

    except Exception as exc:
        print(f"    [FormFillerAI] Claude API error for '{label}': {exc}")
        return options[0] if options else ""


# ---------------------------------------------------------------------------
# Option matching (Levenshtein, no external dependency)
# ---------------------------------------------------------------------------

def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j] + (ca != cb), prev[j + 1] + 1, curr[-1] + 1))
        prev = curr
    return prev[-1]


def find_best_option(answer: str, options: list[str]) -> Optional[str]:
    """Find the closest matching option to the AI-generated answer."""
    if not options:
        return None
    ans = answer.lower().strip()
    skip = {"select an option", "please select", "choose one", "- select -", ""}
    candidates = [o for o in options if o.lower().strip() not in skip]
    if not candidates:
        return options[0] if options else None

    for opt in candidates:
        if opt.lower().strip() == ans:
            return opt
    for opt in candidates:
        if opt.lower().startswith(ans) or ans.startswith(opt.lower().strip()):
            return opt
    for opt in candidates:
        if ans in opt.lower() or opt.lower() in ans:
            return opt

    return min(candidates, key=lambda o: _levenshtein(ans, o.lower().strip()))


# ---------------------------------------------------------------------------
# Honeypot / bot-trap detection
# ---------------------------------------------------------------------------

_HONEYPOT_LABEL_RE = re.compile(
    r"robots?\s+only|do\s+not\s+enter|leave\s+(this\s+)?blank|"
    r"if\s+you'?re\s+human|bot\s+field",
    re.IGNORECASE,
)
_HONEYPOT_NAME_RE = re.compile(
    r"beecatcher|honeypot|honey_pot|hpot|bot.?catch|spam.?trap|"
    r"confirm.?email.?address.?hidden",
    re.IGNORECASE,
)


def _is_honeypot(el, label: str) -> bool:
    """
    Detect anti-bot honeypot fields (e.g. Citi Workday's 'beecatcher' /
    'This input is for robots only'). Filling these flags us as a bot, so skip.
    """
    if label and _HONEYPOT_LABEL_RE.search(label):
        return True
    try:
        for attr in ("name", "id", "data-automation-id", "aria-label"):
            v = el.get_attribute(attr) or ""
            if v and _HONEYPOT_NAME_RE.search(v):
                return True
        # Visually hidden inputs that still report visible are classic honeypots
        style = (el.get_attribute("style") or "").replace(" ", "").lower()
        if "opacity:0" in style or "display:none" in style or "visibility:hidden" in style:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Main page filler
# ---------------------------------------------------------------------------

def fill_page_fields(
    page,
    profile: dict,
    resume_text: str = "",
    skip_filled: bool = True,
    verbose: bool = True,
) -> int:
    """
    Scan visible form fields on `page` and fill each unfilled one.
    Returns number of fields acted on.
    """
    filled = 0

    # ── Text / email / tel / number inputs ──────────────────────────────────
    input_selectors = [
        ("text",   'input[type="text"]:not([readonly]):not([disabled])'),
        ("text",   'input:not([type]):not([readonly]):not([disabled])'),
        ("number", 'input[type="number"]:not([readonly]):not([disabled])'),
        ("email",  'input[type="email"]:not([readonly]):not([disabled])'),
        ("tel",    'input[type="tel"]:not([readonly]):not([disabled])'),
        ("textarea", 'textarea:not([readonly]):not([disabled])'),
    ]

    for ftype, selector in input_selectors:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue

        for el in elements:
            try:
                if not el.is_visible():
                    continue
                if skip_filled:
                    current = el.input_value()
                    if current and current.strip():
                        continue
                label = _get_element_label(page, el)
                if not label:
                    continue
                if _is_honeypot(el, label):
                    if verbose:
                        print(f"    [FormFillerAI] Skipping honeypot field '{label[:40]}'")
                    continue
                answer = get_preset_answer(label, profile)
                if answer is None:
                    answer = ask_claude(label, ftype, [], profile, resume_text)
                if answer:
                    el.fill(str(answer))
                    filled += 1
                    if verbose:
                        print(f"    [FormFillerAI] '{label[:45]}' ← '{str(answer)[:35]}'")
            except Exception as exc:
                if verbose:
                    print(f"    [FormFillerAI] Skip input ({exc.__class__.__name__})")

    # ── Select dropdowns ────────────────────────────────────────────────────
    try:
        selects = page.query_selector_all("select:not([disabled])")
    except Exception:
        selects = []

    for sel_el in selects:
        try:
            if not sel_el.is_visible():
                continue
            label = _get_element_label(page, sel_el)
            options: list[str] = page.evaluate(
                "el => Array.from(el.options).map(o => o.text.trim())", sel_el
            )
            if not options:
                continue
            answer = get_preset_answer(label or "dropdown", profile)
            if answer is None:
                answer = ask_claude(label or "dropdown", "select", options, profile, resume_text)
            match = find_best_option(answer, options)
            if match:
                sel_el.select_option(label=match)
                filled += 1
                if verbose:
                    print(f"    [FormFillerAI] '{label[:45]}' ← '{match[:35]}' (select)")
        except Exception as exc:
            if verbose:
                print(f"    [FormFillerAI] Skip select ({exc.__class__.__name__})")

    return filled


# ---------------------------------------------------------------------------
# Radio button filler (LinkedIn Easy Apply style: fieldset + legend)
# ---------------------------------------------------------------------------

def _radio_option_text(page, r) -> str:
    """Best-effort visible label for a single radio input."""
    lbl_id = r.get_attribute("id")
    if lbl_id:
        lbl = page.query_selector(f'label[for="{lbl_id}"]')
        if lbl:
            t = lbl.inner_text().strip()
            if t:
                return t
    aria = r.get_attribute("aria-label")
    if aria:
        return aria.strip()
    # label ancestor
    try:
        t = r.evaluate("el => { const l = el.closest('label'); return l ? l.innerText : ''; }")
        if t and t.strip():
            return t.strip()
    except Exception:
        pass
    return r.get_attribute("value") or ""


def _radio_group_question(page, first_radio, name: str) -> str:
    """Find the question text for a name-grouped radio set (no fieldset/legend)."""
    # aria-labelledby on the group container or the radio itself
    lbl = _get_element_label(page, first_radio)
    if lbl and lbl.lower() not in ("yes", "no"):
        return lbl
    # Walk up to a container and grab its leading text / a label / legend / [role=group] aria
    try:
        txt = first_radio.evaluate(
            """el => {
                let node = el;
                for (let i = 0; i < 5 && node; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    const grp = node.matches('[role=\\'group\\'],[role=\\'radiogroup\\'],fieldset,'
                              + '[data-automation-id]') ? node : null;
                    if (grp) {
                        const al = grp.getAttribute('aria-label');
                        if (al) return al;
                        const leg = grp.querySelector('legend,label,h2,h3,.gwt-Label,[id$=\\'label\\']');
                        if (leg && leg.innerText.trim()) return leg.innerText;
                    }
                }
                return '';
            }"""
        )
        if txt and txt.strip():
            return txt.strip().rstrip("*").strip()
    except Exception:
        pass
    return name  # last resort: the field name itself (e.g. 'work_auth')


def fill_radio_groups(page, profile: dict, resume_text: str = "") -> int:
    """
    Fill radio button groups on a form page.

    Handles two layouts:
      1. <fieldset><legend>Question</legend> ... </fieldset>  (LinkedIn Easy Apply)
      2. radios sharing a `name` attribute with the question in a nearby label /
         aria-label / container (Workday and most standard HTML forms)
    """
    filled = 0
    handled_names: set = set()

    # ── Layout 1: fieldset + legend ─────────────────────────────────────────
    try:
        for fs in page.query_selector_all("fieldset"):
            try:
                legend = fs.query_selector("legend")
                if not legend:
                    continue
                question = legend.inner_text().strip().rstrip("*").strip()
                radios = fs.query_selector_all('input[type="radio"]')
                if not radios:
                    continue
                options = [(_radio_option_text(page, r), r) for r in radios]
                for r in radios:
                    nm = r.get_attribute("name")
                    if nm:
                        handled_names.add(nm)
                if _select_radio(page, question, options, profile, resume_text):
                    filled += 1
            except Exception:
                pass
    except Exception:
        pass

    # ── Layout 2: radios grouped by `name` attribute ────────────────────────
    try:
        groups: dict = {}
        for r in page.query_selector_all('input[type="radio"]'):
            try:
                if not r.is_visible():
                    continue
                nm = r.get_attribute("name") or ""
                if not nm or nm in handled_names:
                    continue
                groups.setdefault(nm, []).append(r)
            except Exception:
                pass

        for nm, radios in groups.items():
            try:
                options = [(_radio_option_text(page, r), r) for r in radios]
                question = _radio_group_question(page, radios[0], nm)
                if _select_radio(page, question, options, profile, resume_text):
                    filled += 1
            except Exception:
                pass
    except Exception:
        pass

    return filled


def _select_radio(page, question, options, profile, resume_text) -> bool:
    """Choose and click the best radio option for a question. Returns True if clicked."""
    option_texts = [o[0] for o in options if o[0]]
    if not option_texts:
        return False
    answer = get_preset_answer(question, profile)
    if answer is None:
        answer = ask_claude(question, "radio", option_texts, profile, resume_text)
    best = find_best_option(answer, option_texts)
    if not best:
        return False
    for opt_text, radio_el in options:
        if opt_text == best:
            try:
                radio_el.click()
                print(f"    [FormFillerAI] Radio '{question[:40]}' ← '{best[:30]}'")
                return True
            except Exception:
                return False
    return False
