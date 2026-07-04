"""Command-line entry point for the Spotify MCP server.

Usage:
    spotify-mcp            Run the MCP server over stdio (for MCP clients).
    spotify-mcp auth       Authenticate with Spotify from the terminal.
    spotify-mcp auth --force
    spotify-mcp status     Print current authentication status.
"""

from __future__ import annotations

import sys

from .auth import SpotifyAuth, SpotifyAuthError


def _cmd_auth(argv: list[str]) -> int:
    force = "--force" in argv or "-f" in argv
    try:
        result = SpotifyAuth().login(force=force)
    except SpotifyAuthError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 1
    print("Authenticated with Spotify.")
    print(f"  Token cache: {result.get('cache')}")
    print(f"  Scopes: {result.get('scopes')}")
    return 0


def _cmd_status() -> int:
    auth = SpotifyAuth()
    print(f"Client ID configured: {bool(auth.client_id)}")
    print(f"Redirect URI:         {auth.redirect_uri}")
    print(f"Authenticated:        {auth.is_authenticated}")
    print(f"Token cache:          {auth.cache}")
    return 0


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("auth", "login"):
        raise SystemExit(_cmd_auth(argv[1:]))
    if argv and argv[0] == "status":
        raise SystemExit(_cmd_status())
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        raise SystemExit(0)

    # Default: run the MCP server. Import lazily so `auth`/`status` work even if
    # the mcp package isn't fully importable in some minimal environments.
    from .server import run

    run()


if __name__ == "__main__":
    main()
