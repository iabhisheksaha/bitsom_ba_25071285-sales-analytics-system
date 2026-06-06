"""
HIGH-FIDELITY CITI REPLICA - proves WorkdayHandler defeats the exact obstacles
seen in the real Citi run (from the user's screenshots + citi_run.log):

  1. Cookie-consent banner (legalNoticeAcceptButton)
  2. Sign-in MODAL with an invisible click-outside-catcher overlay that
     intercepts pointer events  -> normal .click() times out, JS click wins
  3. A 'beecatcher' HONEYPOT field labelled 'for robots only' -> must NOT fill
  4. Both Sign In AND Create Account forms present -> must Sign In, not Create
  5. Multi-step wizard: Sign In -> My Information -> Application Questions
     (work-auth/sponsorship radios) -> Review -> Submit

PASS (exit 0) requires: cookie dismissed, signed in (not account-created),
honeypot left empty, and a REAL Submit recorded by the server.

Run:  python test_citi_replica.py
"""

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ.setdefault("CHROMIUM_BIN", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
os.environ.setdefault("HEADLESS", "true")
sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright
from agents.agent1_job_discovery import JobPosting
from agents.agent3_application import WorkdayHandler, CHROMIUM_BIN

PORT = 18094
STATE = {
    "cookie_dismissed": False, "signed_in": False, "account_created": False,
    "submitted": False, "beecatcher": "", "step": 0, "untrusted_attempts": 0,
    "chose_manual": False, "chose_linkedin": False,
}

# An invisible full-screen overlay that intercepts pointer events - this is what
# makes Playwright's normal .click() fail with 'intercepts pointer events',
# exactly like Citi's data-behavior-click-outside-close modal.
INTERCEPT_OVERLAY = (
    '<div data-behavior-click-outside-close="topmost" '
    'style="position:fixed;inset:0;z-index:9999;background:transparent;"></div>'
)


# Landing == Citi's /apply page when signed out: cookie banner + sign-in modal
# (both forms) + honeypot + the click-intercepting overlay on top. login() signs
# in here, exactly like the real flow, then the chooser appears.
def page_signin():
    return f"""<html><body>
      <div id="cookie" style="position:fixed;top:0;left:0;right:0;z-index:10001;background:#eee;padding:8px;">
        Cookies. <button data-automation-id="legalNoticeAcceptButton"
          onclick="fetch('/ev?cookie=1');document.getElementById('cookie').remove()">Accept Cookies</button>
      </div>
      <h2>Create Account/Sign In</h2>
      <div class="modal" style="position:relative;z-index:10;">
        <h3>Sign In</h3>
        <form>
          <label>Email Address *</label>
          <input type="email" data-automation-id="email" name="username"/>
          <label>Password *</label>
          <input type="password" data-automation-id="password"/>
          <!-- HONEYPOT: must never be filled -->
          <input type="text" name="beecatcher"
                 aria-label="Enter website. This input is for robots only, do not enter if you're human."/>
          <!-- ANTI-BOT: only a TRUSTED click authenticates. A JS .click()
               (event.isTrusted=false) is ignored, exactly like real Citi. -->
          <button type="button" data-automation-id="signInSubmitButton"
            onclick="if(event.isTrusted){{fetch('/ev?signin=1').then(()=>window.location='/chooser')}}else{{fetch('/ev?untrusted=1')}}">Sign In</button>
        </form>
        <h3>Create Account</h3>
        <form>
          <input type="email" data-automation-id="emailCreate"/>
          <input type="password" data-automation-id="passwordCreate"/>
          <input type="password" data-automation-id="verifyPassword"/>
          <button type="button" data-automation-id="createAccountSubmitButton"
            onclick="fetch('/ev?create=1').then(()=>window.location='/wizard')">Create Account</button>
        </form>
      </div>
      {INTERCEPT_OVERLAY}
      <script>
        // capture honeypot value if anything fills it
        document.querySelector("[name='beecatcher']").addEventListener('input', e=>{{
          fetch('/ev?bee='+encodeURIComponent(e.target.value));
        }});
      </script>
    </body></html>"""


# Post-sign-in 'Start Your Application' chooser. 'Apply With LinkedIn' is a
# DEAD END (mirrors the real run where it didn't progress); only 'Apply Manually'
# advances into the wizard. Proves the bot picks the working path.
def page_chooser():
    # As hostile as real Citi: a click-intercepting overlay covers the buttons AND
    # 'Apply Manually' only advances on a TRUSTED event. So a normal click is
    # intercepted and a JS click is isTrusted=false - only focus+Enter or a trusted
    # coordinate click gets through. 'Apply With LinkedIn' is a dead end.
    return f"""<html><body>
      <h2>Start Your Application</h2>
      <div style="position:relative;z-index:10;">
        <button data-automation-id="autofillWithResume"
          onclick="if(event.isTrusted){{window.location='/wizard'}}">Autofill with Resume</button>
        <button data-automation-id="applyManually"
          onclick="if(event.isTrusted){{fetch('/ev?manual=1').then(()=>window.location='/wizard')}}">Apply Manually</button>
        <button data-automation-id="applyWithLinkedIn"
          onclick="fetch('/ev?linkedin=1')">Apply With LinkedIn</button>
      </div>
      {INTERCEPT_OVERLAY}
    </body></html>"""


def page_wizard(step):
    if step == 0:  # My Information
        body = """
          <h2 data-automation-id="pageHeader">My Information</h2>
          <label for="fn">First Name</label><input id="fn" name="firstName"/>
          <label for="ln">Last Name</label><input id="ln" name="lastName"/>
          <label for="ph">Phone</label><input id="ph" name="phone"/>
          <input type="text" name="hpot2" style="opacity:0;position:absolute;"
                 aria-label="Leave this field blank"/>
        """
    elif step == 1:  # Application Questions
        body = """
          <h2 data-automation-id="pageHeader">Application Questions 1 of 2</h2>
          <div role="group" aria-label="Are you legally authorized to work in India?">
            <input type="radio" id="wa_y" name="work_auth" value="Yes"/><label for="wa_y">Yes</label>
            <input type="radio" id="wa_n" name="work_auth" value="No"/><label for="wa_n">No</label>
          </div>
          <div role="group" aria-label="Will you now or in the future require visa sponsorship?">
            <input type="radio" id="sp_y" name="sponsor" value="Yes"/><label for="sp_y">Yes</label>
            <input type="radio" id="sp_n" name="sponsor" value="No"/><label for="sp_n">No</label>
          </div>
        """
    else:  # Review
        return """<html><body>
          <h2 data-automation-id="pageHeader">Review</h2>
          <button data-automation-id="submitButton"
            onclick="fetch('/ev?submit=1').then(()=>window.location='/done')">Submit</button>
        </body></html>"""
    return f"""<html><body>
      <form>{body}</form>
      <button data-automation-id="bottom-navigation-next-btn"
        onclick="window.location='/wizard'">Save and Continue</button>
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
        p = self.path.split("?")[0]
        q = self.path.split("?")[1] if "?" in self.path else ""
        if p == "/ev":
            if "cookie=1" in q: STATE["cookie_dismissed"] = True
            if "signin=1" in q: STATE["signed_in"] = True
            if "create=1" in q: STATE["account_created"] = True
            if "submit=1" in q: STATE["submitted"] = True
            if "untrusted=1" in q: STATE["untrusted_attempts"] += 1
            if "manual=1" in q: STATE["chose_manual"] = True
            if "linkedin=1" in q: STATE["chose_linkedin"] = True
            if "bee=" in q: STATE["beecatcher"] = q.split("bee=", 1)[1]
            self._send("ok"); return
        if p == "/signin":
            self._send(page_signin()); return
        if p == "/chooser":
            self._send(page_chooser()); return
        if p == "/wizard":
            html = page_wizard(STATE["step"]); STATE["step"] += 1
            self._send(html); return
        if p == "/done":
            self._send("<html><body><h1>Application submitted</h1></body></html>"); return
        # default (/landing) IS the signed-out apply page with the sign-in modal
        self._send(page_signin())

    def log_message(self, *_):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", PORT), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)

    resume = Path("resume/base_resume.docx")
    if not resume.exists():
        print("FAIL: resume/base_resume.docx missing"); sys.exit(1)

    chromium = CHROMIUM_BIN if Path(CHROMIUM_BIN).exists() else None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"],
            **({"executable_path": chromium} if chromium else {}),
        )
        page = browser.new_context().new_page()
        page.goto(f"http://127.0.0.1:{PORT}/landing", wait_until="domcontentloaded")

        handler = WorkdayHandler(page)
        job = JobPosting(platform="workday", title="Technology Lead BA - VP",
                         company="Citi", location="Pune",
                         url=f"http://127.0.0.1:{PORT}/landing")

        print("== login() ==")
        handler.login("abhisheksaha@live.com", "AV33Rs@r@")
        print("== apply() ==")
        result = handler.apply(job, resume)
        print(f"apply() returned: {result}")
        browser.close()
    srv.shutdown()

    print("\n== Results vs the real Citi obstacles ==")
    checks = [
        ("Cookie banner dismissed",          STATE["cookie_dismissed"]),
        ("Signed in via TRUSTED click",       STATE["signed_in"] and not STATE["account_created"]),
        ("Chose 'Apply Manually' (not the LinkedIn dead-end)",
                                              STATE["chose_manual"] and not STATE["chose_linkedin"]),
        ("Honeypot 'beecatcher' left empty",  STATE["beecatcher"] == ""),
        ("Reached real Submit",               STATE["submitted"]),
    ]
    print(f"  (untrusted JS-click attempts Citi would have ignored: {STATE['untrusted_attempts']})")
    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    if ok and result:
        print("\nPASS: WorkdayHandler defeated every real Citi obstacle and submitted.")
        sys.exit(0)
    print(f"\nFAIL. State={STATE}")
    sys.exit(1)


if __name__ == "__main__":
    main()
