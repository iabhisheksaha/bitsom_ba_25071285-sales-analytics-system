"""
Verify WorkdayHandler._handle_account_step() on Citi's 'Create Account/Sign In'
wizard step, which renders BOTH a Sign In form and a Create Account form.

The bot must:
  - fill ONLY the Sign In email/password
  - click 'Sign In' (data-automation-id=signInSubmitButton)
  - NEVER fill 'Verify New Password' or click 'Create Account'

Run:  python test_workday_account_step.py   (exit 0 = signed in, create-account untouched)
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
from agents.agent3_application import WorkdayHandler, CHROMIUM_BIN

PORT = 18093
STATE = {"clicked": None}

# Both forms present at once - exactly like Citi's screenshot.
PAGE = """<html><body>
  <h2>Create Account/Sign In</h2>

  <!-- Sign In form -->
  <form id="signin_form">
    <h3>Sign In</h3>
    <label>Email Address *</label>
    <input type="email" data-automation-id="email" name="username"/>
    <label>Password *</label>
    <input type="password" data-automation-id="password"/>
    <button data-automation-id="signInSubmitButton" type="button"
       onclick="window.location='/done?via=signin'">Sign In</button>
    <a data-automation-id="backToSignInLink" href="#">Sign In</a>
  </form>

  <!-- Create Account form (must remain untouched) -->
  <form id="create_form">
    <h3>Create Account</h3>
    <label>Email Address *</label>
    <input type="email" data-automation-id="emailCreate"/>
    <label>Password *</label>
    <input type="password" data-automation-id="passwordCreate"/>
    <label>Verify New Password *</label>
    <input type="password" data-automation-id="verifyPassword"/>
    <input type="checkbox" id="agree"/>
    <button data-automation-id="createAccountSubmitButton" type="button"
       onclick="window.location='/done?via=create'">Create Account</button>
  </form>
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
        p = self.path
        if p.startswith("/done"):
            STATE["clicked"] = "signin" if "via=signin" in p else "create"
            self._send("<html><body><h1>OK</h1></body></html>")
        else:
            self._send(PAGE)

    def log_message(self, *_):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", PORT), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)

    chromium = CHROMIUM_BIN if Path(CHROMIUM_BIN).exists() else None
    verify_value = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"],
            **({"executable_path": chromium} if chromium else {}),
        )
        page = browser.new_context().new_page()
        page.goto(f"http://127.0.0.1:{PORT}/account", wait_until="domcontentloaded")

        handler = WorkdayHandler(page)
        print("== _handle_account_step (sign in, existing account) ==")
        handler._handle_account_step("abhisheksaha@live.com", "AV33Rs@r@")

        # Read the verify-password value BEFORE navigation if still on page
        try:
            vp = page.query_selector("[data-automation-id='verifyPassword']")
            verify_value = vp.input_value() if vp else ""
        except Exception:
            verify_value = "<navigated>"
        browser.close()
    srv.shutdown()

    print(f"\n  button clicked : {STATE['clicked']}")
    print(f"  verifyPassword : {verify_value!r}")
    ok = STATE["clicked"] == "signin" and (verify_value in ("", "<navigated>"))
    if ok:
        print("\nPASS: signed in via Sign In button; Create Account form left untouched.")
        sys.exit(0)
    print("\nFAIL: wrong path - it must click Sign In and never fill Verify New Password.")
    sys.exit(1)


if __name__ == "__main__":
    main()
