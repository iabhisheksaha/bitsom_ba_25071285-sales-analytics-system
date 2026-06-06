"""
Verify WorkdayHandler.login() handles a POPUP-window sign-in (Citi's pattern).
The 'Sign In' link calls window.open() to a separate popup page that holds the
email/password form. On submit the popup sets the opener back to the apply page
and closes itself - exactly like Citi Workday.

Run:  python test_workday_popup.py    (exit 0 = login succeeded via popup)
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

PORT = 18092
STATE = {"signed_in": False}

# Landing page: NO email field; only a 'Sign In' link that opens a popup window.
LANDING = f"""<html><body>
  <h1>Citi Careers - Technology Lead BA</h1>
  <a data-automation-id="signInLink" href="#"
     onclick="window.open('http://127.0.0.1:{PORT}/popup','wd_signin','width=500,height=600');return false;">
     Sign In</a>
</body></html>"""

# Popup window: the actual sign-in form (separate Page in Playwright).
POPUP = f"""<html><body>
  <h2>Sign In</h2>
  <input type="email" data-automation-id="email" />
  <input type="password" data-automation-id="password" />
  <button data-automation-id="signInSubmitButton"
     onclick="if(window.opener){{window.opener.location='http://127.0.0.1:{PORT}/apply';}}window.close();">
     Sign In</button>
</body></html>"""

APPLIED = """<html><body><h1>Signed in</h1>
  <button data-automation-id="applyButton">Apply</button></body></html>"""


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
        if p == "/popup":
            self._send(POPUP)
        elif p == "/apply":
            STATE["signed_in"] = True
            self._send(APPLIED)
        else:
            self._send(LANDING)

    def log_message(self, *_):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", PORT), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)

    chromium = CHROMIUM_BIN if Path(CHROMIUM_BIN).exists() else None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"],
            **({"executable_path": chromium} if chromium else {}),
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{PORT}/landing", wait_until="domcontentloaded")

        handler = WorkdayHandler(page)
        print("== Login via popup ==")
        ok = handler.login("abhishek@test.com", "testpass")
        print(f"login() returned: {ok}")
        page.wait_for_timeout(1500)
        browser.close()
    srv.shutdown()

    print(f"\n  signed_in (server saw /apply): {STATE['signed_in']}")
    if ok and STATE["signed_in"]:
        print("\nPASS: login() entered credentials in the popup and authenticated.")
        sys.exit(0)
    print("\nFAIL: popup login did not complete.")
    sys.exit(1)


if __name__ == "__main__":
    main()
