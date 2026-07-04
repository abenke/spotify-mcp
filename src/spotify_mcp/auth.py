"""OAuth 2.0 authorization for the Spotify Web API.

Implements the Authorization Code with PKCE flow (the recommended flow for
locally-run apps that cannot safely store a client secret). If a client secret
is provided via ``SPOTIFY_CLIENT_SECRET``, the classic Authorization Code flow
is used instead.

Tokens are cached on disk and refreshed automatically before expiry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import httpx

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# Scopes needed to view the user's playlists (public, private, collaborative),
# create/modify playlists, and read the user's profile.
DEFAULT_SCOPES = [
    "user-read-private",
    "user-read-email",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
]

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"


class SpotifyAuthError(RuntimeError):
    """Raised for configuration or authorization failures."""


def _cache_path() -> Path:
    override = os.environ.get("SPOTIFY_MCP_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".spotify-mcp" / "token.json"


def _generate_pkce_pair() -> tuple[str, str]:
    """Return a (code_verifier, code_challenge) pair for PKCE (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler that captures the OAuth redirect query parameters."""

    # Populated by the server instance.
    result: dict = {}
    done: Optional[threading.Event] = None

    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        # Ignore favicon or unrelated requests.
        if "code" not in params and "error" not in params:
            self.send_response(404)
            self.end_headers()
            return

        type(self).result.update({k: v[0] for k, v in params.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "error" in params:
            body = (
                "<html><body style='font-family:sans-serif'>"
                "<h2>Spotify authorization failed</h2>"
                f"<p>{params['error'][0]}</p>"
                "<p>You can close this tab and try again.</p></body></html>"
            )
        else:
            body = (
                "<html><body style='font-family:sans-serif'>"
                "<h2>Spotify authorization complete</h2>"
                "<p>You can close this tab and return to your MCP client.</p>"
                "</body></html>"
            )
        self.wfile.write(body.encode("utf-8"))
        if type(self).done is not None:
            type(self).done.set()

    def log_message(self, *args):  # silence default stderr logging
        return


class SpotifyAuth:
    """Manages Spotify OAuth tokens (acquire, cache, refresh)."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        scopes: Optional[list[str]] = None,
    ):
        self.client_id = client_id or os.environ.get("SPOTIFY_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("SPOTIFY_CLIENT_SECRET")
        self.redirect_uri = (
            redirect_uri
            or os.environ.get("SPOTIFY_REDIRECT_URI")
            or DEFAULT_REDIRECT_URI
        )
        self.scopes = scopes or DEFAULT_SCOPES
        self.cache = _cache_path()
        self._lock = threading.Lock()
        self._token: Optional[dict] = self._load()

    # ---- persistence -------------------------------------------------------

    def _load(self) -> Optional[dict]:
        try:
            with open(self.cache, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save(self) -> None:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._token, fh, indent=2)
        # Restrict permissions on the token file where the OS supports it.
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.cache)

    # ---- state -------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def _require_client_id(self) -> str:
        if not self.client_id:
            raise SpotifyAuthError(
                "SPOTIFY_CLIENT_ID is not set. Create a Spotify app at "
                "https://developer.spotify.com/dashboard, then set SPOTIFY_CLIENT_ID "
                "(and register the redirect URI) in your environment."
            )
        return self.client_id

    # ---- token acquisition -------------------------------------------------

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing it if necessary."""
        with self._lock:
            if not self._token:
                raise SpotifyAuthError(
                    "Not authenticated with Spotify. Run the `authenticate` tool "
                    "(or `spotify-mcp auth` on the command line) to sign in."
                )
            if self._token.get("expires_at", 0) - time.time() < 60:
                self._refresh_locked()
            return self._token["access_token"]

    def _store_token_response(self, payload: dict) -> None:
        token = self._token or {}
        token["access_token"] = payload["access_token"]
        token["token_type"] = payload.get("token_type", "Bearer")
        token["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
        if payload.get("scope"):
            token["scope"] = payload["scope"]
        # Refresh tokens may be rotated; keep the old one if a new one isn't sent.
        if payload.get("refresh_token"):
            token["refresh_token"] = payload["refresh_token"]
        self._token = token
        self._save()

    def _token_request_headers(self) -> dict:
        # Classic flow authenticates the client with HTTP Basic; PKCE does not.
        if self.client_secret:
            raw = f"{self.client_id}:{self.client_secret}".encode("ascii")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        return {}

    def _refresh_locked(self) -> None:
        refresh_token = (self._token or {}).get("refresh_token")
        if not refresh_token:
            raise SpotifyAuthError(
                "No refresh token available. Re-run authentication to sign in again."
            )
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._require_client_id(),
        }
        resp = httpx.post(
            TOKEN_URL, data=data, headers=self._token_request_headers(), timeout=30
        )
        if resp.status_code != 200:
            raise SpotifyAuthError(
                f"Failed to refresh Spotify token ({resp.status_code}): {resp.text}"
            )
        self._store_token_response(resp.json())

    # ---- interactive login -------------------------------------------------

    def login(self, force: bool = False, timeout: int = 300) -> dict:
        """Run the interactive browser-based authorization flow.

        Opens the system browser to Spotify's consent page and starts a local
        HTTP server to receive the redirect. Returns a small status dict.
        """
        client_id = self._require_client_id()

        parsed = urllib.parse.urlparse(self.redirect_uri)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80

        use_pkce = not self.client_secret
        verifier, challenge = _generate_pkce_pair()
        state = secrets.token_urlsafe(24)

        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
        }
        if use_pkce:
            params["code_challenge_method"] = "S256"
            params["code_challenge"] = challenge
        if force:
            params["show_dialog"] = "true"
        auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)

        # Fresh per-attempt state on the handler class.
        _CallbackHandler.result = {}
        _CallbackHandler.done = threading.Event()

        try:
            server = HTTPServer((host, port), _CallbackHandler)
        except OSError as exc:
            raise SpotifyAuthError(
                f"Could not start local callback server on {host}:{port} ({exc}). "
                "Make sure the port is free and matches SPOTIFY_REDIRECT_URI."
            ) from exc

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        opened = webbrowser.open(auth_url)
        try:
            got_it = _CallbackHandler.done.wait(timeout=timeout)
        finally:
            server.shutdown()
            server.server_close()

        if not got_it:
            raise SpotifyAuthError(
                "Timed out waiting for Spotify authorization. If a browser did not "
                f"open, visit this URL manually:\n{auth_url}"
            )

        result = _CallbackHandler.result
        if "error" in result:
            raise SpotifyAuthError(f"Spotify returned an error: {result['error']}")
        if result.get("state") != state:
            raise SpotifyAuthError("State mismatch during authorization (possible CSRF).")
        code = result.get("code")
        if not code:
            raise SpotifyAuthError("No authorization code received from Spotify.")

        self._exchange_code(code, verifier if use_pkce else None)
        _ = opened  # browser may already have been open; not an error either way
        return {
            "status": "authenticated",
            "scopes": self._token.get("scope") if self._token else None,
            "cache": str(self.cache),
        }

    def _exchange_code(self, code: str, verifier: Optional[str]) -> None:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self._require_client_id(),
        }
        if verifier is not None:
            data["code_verifier"] = verifier
        resp = httpx.post(
            TOKEN_URL, data=data, headers=self._token_request_headers(), timeout=30
        )
        if resp.status_code != 200:
            raise SpotifyAuthError(
                f"Failed to exchange authorization code ({resp.status_code}): {resp.text}"
            )
        with self._lock:
            self._token = None  # start clean so refresh_token is taken from payload
            self._store_token_response(resp.json())
