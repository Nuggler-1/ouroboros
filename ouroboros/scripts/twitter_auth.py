#!/usr/bin/env python3
"""
Twitter / X OAuth2 PKCE authentication script.

Designed to run on the Ouroboros server (178.215.236.146).
Starts a temporary HTTP callback server on port 8080, prints an
authorization URL, waits for the owner to open it in a browser
logged in as @ouro_agent, then exchanges the code for access +
refresh tokens and persists them to Drive and the server.

Usage (on the server):
    python3 ouroboros/scripts/twitter_auth.py

Required environment variables (or .env on the server):
    TWITTER_CLIENT_ID      – OAuth2 App Client ID
    TWITTER_CLIENT_SECRET  – OAuth2 App Client Secret

Optional:
    TWITTER_REDIRECT_URI   – defaults to http://178.215.236.146:8080/callback
    TWITTER_CALLBACK_PORT  – port for the local callback server (default 8080)
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

CLIENT_ID     = os.environ.get("TWITTER_CLIENT_ID",     "c2FHU3E0VXFrbFZaNThoaTk2bmU6MTpjaQ")
CLIENT_SECRET = os.environ.get("TWITTER_CLIENT_SECRET", "")
REDIRECT_URI  = os.environ.get("TWITTER_REDIRECT_URI",  "http://178.215.236.146:8080/callback")
CALLBACK_PORT = int(os.environ.get("TWITTER_CALLBACK_PORT", "8080"))

SCOPES = "tweet.read tweet.write users.read offline.access list.read"

TOKEN_FILE    = Path("/root/.ouroboros/twitter_tokens.json")
DRIVE_TOKEN   = Path("/content/drive/MyDrive/Ouroboros/memory/sensitive/twitter_oauth2_tokens.json")

# ── PKCE helpers ───────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_S256)."""
    verifier  = secrets.token_urlsafe(64)          # 86 chars, well within 43-128
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge

# ── Callback HTTP server ───────────────────────────────────────────────────────

_callback_result: dict = {}
_server_ready    = threading.Event()

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default logs
        pass

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if parsed.path == "/callback":
            if "code" in params:
                _callback_result["code"]  = params["code"]
                _callback_result["state"] = params.get("state", "")
                body = b"<h2>Authorized! You can close this tab.</h2>"
                self.send_response(200)
            else:
                _callback_result["error"] = params.get("error", "unknown")
                body = b"<h2>Authorization failed. Check the terminal.</h2>"
                self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()


def _start_server() -> http.server.HTTPServer:
    srv = http.server.HTTPServer(("0.0.0.0", CALLBACK_PORT), _CallbackHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _server_ready.set()
    return srv

# ── Token exchange ─────────────────────────────────────────────────────────────

def exchange_code(code: str, verifier: str) -> dict:
    """POST to Twitter token endpoint and return JSON response."""
    data = urllib.parse.urlencode({
        "code":          code,
        "grant_type":    "authorization_code",
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": verifier,
        "client_id":     CLIENT_ID,
    }).encode()

    # Basic auth header (client_id:client_secret, base64-encoded)
    if CLIENT_SECRET:
        credentials = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
        }
    else:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

    req = urllib.request.Request(
        "https://api.twitter.com/2/oauth2/token",
        data=data,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

# ── Persist tokens ─────────────────────────────────────────────────────────────

def save_tokens(tokens: dict) -> None:
    payload = json.dumps(tokens, indent=2)

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(payload)
    TOKEN_FILE.chmod(0o600)
    print(f"✅ Tokens saved → {TOKEN_FILE}")

    try:
        DRIVE_TOKEN.parent.mkdir(parents=True, exist_ok=True)
        DRIVE_TOKEN.write_text(payload)
        DRIVE_TOKEN.chmod(0o600)
        print(f"✅ Tokens saved → {DRIVE_TOKEN}")
    except Exception as exc:
        print(f"⚠️  Could not write to Drive: {exc}")

    # Also update /root/.bashrc so subsequent bird.fast calls pick them up
    access_token = tokens.get("access_token", "")
    _patch_bashrc(access_token)


def _patch_bashrc(access_token: str) -> None:
    bashrc = Path("/root/.bashrc")
    lines = bashrc.read_text().splitlines() if bashrc.exists() else []
    new_lines = [l for l in lines if not l.startswith("export TWITTER_BEARER_TOKEN=")]
    new_lines.append(f"export TWITTER_BEARER_TOKEN={access_token}")
    bashrc.write_text("\n".join(new_lines) + "\n")
    print("✅ ~/.bashrc updated with TWITTER_BEARER_TOKEN")

# ── Main flow ──────────────────────────────────────────────────────────────────

def main():
    if not CLIENT_SECRET:
        print("⚠️  TWITTER_CLIENT_SECRET not set. Token exchange may fail for confidential clients.")
        print("   Set it via:  export TWITTER_CLIENT_SECRET='your_secret'\n")

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)

    params = urllib.parse.urlencode({
        "response_type":         "code",
        "client_id":             CLIENT_ID,
        "redirect_uri":          REDIRECT_URI,
        "scope":                 SCOPES,
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"https://twitter.com/i/oauth2/authorize?{params}"

    print("─" * 70)
    print("  Twitter OAuth2 PKCE — Ouroboros")
    print("─" * 70)
    print(f"\n  Callback server: http://0.0.0.0:{CALLBACK_PORT}/callback")
    print(f"  Redirect URI   : {REDIRECT_URI}\n")

    srv = _start_server()
    _server_ready.wait()
    print("  ✅ Callback server is listening.\n")

    print("  ┌─ ACTION REQUIRED ─────────────────────────────────────────────┐")
    print("  │ Open this URL in a browser logged in as @ouro_agent:          │")
    print("  │                                                                │")
    # Print long URL on its own line for easy copy-paste
    print(f"\n  {auth_url}\n")
    print("  └────────────────────────────────────────────────────────────────┘\n")
    print("  Waiting for callback (up to 300 seconds)…")

    deadline = time.time() + 300
    while not _callback_result and time.time() < deadline:
        time.sleep(0.5)

    if not _callback_result:
        print("\n❌ Timed out waiting for callback. Exiting.")
        return

    if "error" in _callback_result:
        print(f"\n❌ Authorization error: {_callback_result['error']}")
        return

    received_state = _callback_result.get("state", "")
    if received_state != state:
        print(f"\n❌ State mismatch! Expected {state!r}, got {received_state!r}")
        return

    print("  ✅ Callback received. Exchanging code for tokens…")
    code = _callback_result["code"]

    try:
        tokens = exchange_code(code, verifier)
    except Exception as exc:
        print(f"\n❌ Token exchange failed: {exc}")
        return

    print("\n  ✅ Tokens received:")
    print(f"     access_token  : {tokens.get('access_token', '')[:20]}…")
    print(f"     refresh_token : {tokens.get('refresh_token', '')[:20]}…")
    print(f"     expires_in    : {tokens.get('expires_in', 'N/A')} seconds")
    print(f"     scope         : {tokens.get('scope', 'N/A')}\n")

    save_tokens(tokens)

    print("\n  ✅ Authentication complete. You can now use Twitter API tools.\n")
    srv.shutdown()


if __name__ == "__main__":
    main()
