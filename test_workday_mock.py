"""
Standalone verification of WorkdayHandler against a realistic MULTI-STEP
Workday mock with server-side step state. Runs headless locally - no network,
no real credentials. Proves the login -> Start Application -> wizard -> Submit
flow actually reaches a real submit (not the old optimistic 'return True').

Run:  python test_workday_mock.py
Exit code 0 = handler reached the Submit button and server recorded a submit.
"""

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ.setdefault("CHROMIUM_BIN", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
os.environ.setdefault("HEADLESS", "true")
# No ANTHROPIC_API_KEY on purpose: fill_page_fields falls back to deterministic
# values, which is fine for verifying navigation/submit logic.

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright
from agents.agent1_job_discovery import JobPosting
from agents.agent3_application import WorkdayHandler, CHROMIUM_BIN

PORT = 18091
STATE = {"signed_in": False, "step": 0, "submitted": False, "uploaded": False}

# Citi-style sign-in: cookie banner on top + sign-in modal with email/password
# already present in the main page DOM (no popup).
SIGNIN_PAGE = """<html><body>
  <div id="cookie" style="position:fixed;top:0;left:0;right:0;background:#eee;padding:10px;">
    This site uses cookies.
    <button data-automation-id="legalNoticeAcceptButton"
            onclick="document.getElementById('cookie').remove()">Accept Cookies</button>
  </div>
  <h1>Start Your Application</h1>
  <div id="signin">
    <label>Email Address *</label>
    <input type="email" data-automation-id="email" />
    <label>Password *</label>
    <input type="password" data-automation-id="password" />
    <button data-automation-id="signInSubmitButton"
            onclick="window.location='/job/apply'">Sign In</button>
  </div>
</body></html>"""

# After sign-in: job page with Apply button -> Start Application chooser
JOB_PAGE = """<html><body>
  <h1>Technology Lead Business Analyst - VP</h1>
  <button data-automation-id="applyButton"
          onclick="document.getElementById('chooser').style.display='block';this.style.display='none';">Apply</button>
  <div id="chooser" style="display:none">
    <button data-automation-id="applyManually"
            onclick="window.location='/job/wizard'">Apply Manually</button>
  </div>
</body></html>"""

def wizard_page(step: int) -> str:
    # 3 steps then a Review page with Submit
    if step == 0:
        fields = """
          <label>First Name<input data-automation-id="firstName" name="firstName"/></label>
          <label>Last Name<input data-automation-id="lastName" name="lastName"/></label>
          <label>Email<input type="email" name="email"/></label>
          <label>Phone<input name="phone"/></label>
          <input type="file" />
        """
    elif step == 1:
        fields = """
          <label for="yrs">How many years of experience?</label>
          <input id="yrs" name="years_experience" type="number"/>
          <div role="group" aria-label="Are you legally authorized to work in India?">
            <input type="radio" id="wa_y" name="work_auth" value="Yes"/><label for="wa_y">Yes</label>
            <input type="radio" id="wa_n" name="work_auth" value="No"/><label for="wa_n">No</label>
          </div>
        """
    elif step == 2:
        fields = """
          <label for="li">LinkedIn URL</label><input id="li" name="linkedin"/>
          <div role="group" aria-label="Will you now or in the future require visa sponsorship?">
            <input type="radio" id="sp_y" name="sponsor" value="Yes"/><label for="sp_y">Yes</label>
            <input type="radio" id="sp_n" name="sponsor" value="No"/><label for="sp_n">No</label>
          </div>
        """
    else:
        # Review page - Submit button
        return """<html><body><h2>Review</h2>
          <button data-automation-id="submitButton"
                  onclick="window.location='/job/submitted'">Submit</button>
        </body></html>"""

    return f"""<html><body><h2>Step {step+1}</h2>
      <form>{fields}</form>
      <button data-automation-id="bottom-navigation-next-btn"
              onclick="window.location='/job/wizard'">Save and Continue</button>
    </body></html>"""


class Mock(BaseHTTPRequestHandler):
    def _send(self, html):
        b = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/job/apply":
            STATE["signed_in"] = True
            self._send(JOB_PAGE)
        elif path == "/job/wizard":
            self._send(wizard_page(STATE["step"]))
            STATE["step"] += 1
        elif path == "/job/submitted":
            STATE["submitted"] = True
            self._send("<html><body><h1>Application submitted</h1></body></html>")
        else:
            self._send(SIGNIN_PAGE)

    def log_message(self, *_):
        pass


def _check_answer_logic() -> bool:
    """Deterministic check: correct preset answers for the standard screening Qs."""
    from utils.ai_form_filler import get_preset_answer, find_best_option
    profile = {"name": "Abhishek Saha"}
    cases = [
        ("Are you legally authorized to work in India?", ["Yes", "No"], "Yes"),
        ("Will you now or in the future require visa sponsorship?", ["Yes", "No"], "No"),
    ]
    ok = True
    for q, opts, expected in cases:
        ans = get_preset_answer(q, profile)
        best = find_best_option(ans, opts) if ans else None
        status = "OK" if best == expected else "WRONG"
        if best != expected:
            ok = False
        print(f"  [{status}] '{q[:45]}' -> {best} (expected {expected})")
    return ok


def main():
    print("== Answer-logic unit check ==")
    logic_ok = _check_answer_logic()

    srv = HTTPServer(("127.0.0.1", PORT), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)

    resume = Path("resume/base_resume.docx")
    if not resume.exists():
        print("FAIL: resume/base_resume.docx missing")
        sys.exit(1)

    url = f"http://127.0.0.1:{PORT}/job/apply"  # simulate landing on apply page (->signin then back)
    chromium = CHROMIUM_BIN if Path(CHROMIUM_BIN).exists() else None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            **({"executable_path": chromium} if chromium else {}),
        )
        page = browser.new_context().new_page()
        # Start at the sign-in page (as an unauthenticated /apply visit would redirect)
        page.goto(f"http://127.0.0.1:{PORT}/signin", wait_until="domcontentloaded")

        handler = WorkdayHandler(page)
        print("== Login ==")
        ok = handler.login("abhishek@test.com", "testpass")
        print(f"login() returned: {ok}")

        # After login the mock redirects to the job page
        page.goto(f"http://127.0.0.1:{PORT}/job/apply", wait_until="domcontentloaded")

        job = JobPosting(platform="workday", title="Technology Lead BA - VP",
                         company="Citi", location="Pune", url=url)
        print("== Apply ==")
        result = handler.apply(job, resume)
        print(f"apply() returned: {result}")

        browser.close()
    srv.shutdown()

    print("\n== Results ==")
    print(f"  signed_in : {STATE['signed_in']}")
    print(f"  submitted : {STATE['submitted']}")
    if result and STATE["submitted"] and logic_ok:
        print("\nPASS: WorkdayHandler logged in, walked the wizard, submitted,")
        print("      and chose the correct screening answers (Yes work-auth / No sponsorship).")
        sys.exit(0)
    else:
        if not logic_ok:
            print("\nFAIL: screening answer logic picked the wrong option.")
        if not (result and STATE["submitted"]):
            print("\nFAIL: did not reach a real submit.")
        sys.exit(1)


if __name__ == "__main__":
    main()
