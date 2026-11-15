"""Run on YOUR OWN computer (not on the server).

Opens a real browser on Pocket Option. You log in yourself: this script never sees your email or password.
It only reads the WebSocket authentication frame that the site sends after login, prints it, and copies it
to your clipboard when possible. Paste it in the "Compte Pocket Option" field of the iMi BMAX mini app.

The session is a credential for your trading account. Do not share it with anyone but the app you trust,
and log out of Pocket Option in your browser if you want to invalidate it.

    pip install playwright && playwright install chromium
    python capture_ssid.py [--url https://pocketoption.com/en/login/] [--timeout 300]
"""
import argparse
import sys

from playwright.sync_api import sync_playwright

AUTH_PREFIX = '42["auth"'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://pocketoption.com/en/login/")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for you to log in")
    args = ap.parse_args()
    found = {}

    def on_ws(ws):
        def on_sent(payload):
            if isinstance(payload, str) and payload.startswith(AUTH_PREFIX):
                found["ssid"] = payload
        ws.on("framesent", on_sent)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_context().new_page()
        page.on("websocket", on_ws)
        page.goto(args.url)
        print("Connecte-toi dans la fenêtre du navigateur (le script attend jusqu'à %d s)..." % args.timeout)
        waited = 0
        while "ssid" not in found and waited < args.timeout:
            page.wait_for_timeout(500)
            waited += 0.5
        browser.close()

    if "ssid" not in found:
        print("Aucune trame d'authentification capturée.", file=sys.stderr)
        sys.exit(1)
    ssid = found["ssid"]
    print("\nSession capturée. Colle ceci dans l'application :\n")
    print(ssid)
    try:
        import subprocess
        for cmd in (["pbcopy"], ["clip"], ["xclip", "-selection", "clipboard"]):
            try:
                subprocess.run(cmd, input=ssid.encode(), check=True)
                print("\n(copié dans le presse-papiers)")
                break
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
    except Exception:
        pass


if __name__ == "__main__":
    main()
