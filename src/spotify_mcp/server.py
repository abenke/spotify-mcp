"""The Spotify MCP server.

Exposes tools for authenticating, searching Spotify's catalog, viewing tracks
and playlists, and creating/populating playlists.
"""

from __future__ import annotations

import functools
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .auth import SpotifyAuth, SpotifyAuthError
from .client import SpotifyAPIError, SpotifyClient

mcp = FastMCP(
    "spotify",
    instructions=(
        "Tools for working with Spotify: search the catalog, view song and "
        "playlist details, list the signed-in user's playlists, and create new "
        "playlists. If a tool reports that you are not authenticated, call the "
        "`authenticate` tool first (it opens a browser window to sign in)."
    ),
)

# Lazily-constructed singletons so import never fails when env vars are missing.
_auth: Optional[SpotifyAuth] = None
_client: Optional[SpotifyClient] = None


def _get_auth() -> SpotifyAuth:
    global _auth
    if _auth is None:
        _auth = SpotifyAuth()
    return _auth


def _get_client() -> SpotifyClient:
    global _client
    if _client is None:
        _client = SpotifyClient(_get_auth())
    return _client


def _guard(fn):
    """Convert internal exceptions into clean, model-readable error strings.

    ``functools.wraps`` sets ``__wrapped__`` so FastMCP's signature
    introspection sees the real parameters rather than ``*args, **kwargs``.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (SpotifyAuthError, SpotifyAPIError) as exc:
            return {"error": str(exc)}

    return wrapper


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@mcp.tool()
@_guard
def authenticate(force: bool = False) -> dict:
    """Sign in to Spotify via OAuth (opens a browser window).

    Run this once before using the other tools. Tokens are cached locally and
    refreshed automatically, so you normally only need to authenticate a single
    time per machine.

    Args:
        force: If true, force Spotify to re-show the consent screen (useful to
            switch accounts or re-grant scopes).
    """
    return _get_auth().login(force=force)


@mcp.tool()
@_guard
def auth_status() -> dict:
    """Report whether the server is currently authenticated with Spotify."""
    auth = _get_auth()
    return {
        "authenticated": auth.is_authenticated,
        "client_id_configured": bool(auth.client_id),
        "redirect_uri": auth.redirect_uri,
        "scopes": auth.scopes,
    }


@mcp.tool()
@_guard
def get_current_user() -> dict:
    """Get the profile of the signed-in Spotify user (id, name, country, etc.)."""
    return _get_client().current_user_summary()


# --------------------------------------------------------------------------
# Search & song details
# --------------------------------------------------------------------------


@mcp.tool()
@_guard
def search(query: str, types: str = "track", limit: int = 10) -> dict:
    """Search the Spotify catalog.

    Args:
        query: Search text. Supports Spotify field filters, e.g.
            `artist:radiohead track:creep` or `year:2020 genre:jazz`.
        types: Comma-separated result types to return. Any of:
            `track`, `album`, `artist`, `playlist`. Defaults to `track`.
        limit: Max results per type (1-50). Defaults to 10.
    """
    type_list = [t.strip() for t in types.split(",") if t.strip()]
    valid = {"track", "album", "artist", "playlist"}
    invalid = [t for t in type_list if t not in valid]
    if invalid:
        return {"error": f"Invalid search type(s): {invalid}. Valid: {sorted(valid)}"}
    if not type_list:
        type_list = ["track"]
    return _get_client().search(query, type_list, limit=limit)


@mcp.tool()
@_guard
def get_track(track_id: str) -> dict:
    """Get detailed information about a single song.

    Args:
        track_id: A Spotify track ID, `spotify:track:...` URI, or
            open.spotify.com track URL.
    """
    return _get_client().get_track(track_id)


# --------------------------------------------------------------------------
# Playlists
# --------------------------------------------------------------------------


@mcp.tool()
@_guard
def list_my_playlists(limit: int = 20, offset: int = 0) -> dict:
    """List the playlists owned or followed by the signed-in user.

    Args:
        limit: Number of playlists to return (1-50). Defaults to 20.
        offset: Index to start from, for paging through large libraries.
    """
    return _get_client().list_my_playlists(limit=limit, offset=offset)


@mcp.tool()
@_guard
def get_playlist(playlist_id: str, tracks_limit: int = 100) -> dict:
    """Get a playlist's details and its tracks.

    Args:
        playlist_id: A Spotify playlist ID, `spotify:playlist:...` URI, or
            open.spotify.com playlist URL.
        tracks_limit: Max number of tracks to include (default 100). If the
            playlist has more, `tracks_truncated` will be true.
    """
    return _get_client().get_playlist(playlist_id, tracks_limit=tracks_limit)


@mcp.tool()
@_guard
def create_playlist(
    name: str,
    public: bool = False,
    description: str = "",
    collaborative: bool = False,
    track_uris: Optional[list[str]] = None,
) -> dict:
    """Create a new playlist for the signed-in user.

    Args:
        name: The playlist name.
        public: Whether the playlist is public. Defaults to false (private).
        description: Optional playlist description.
        collaborative: If true, others can edit it (forces the playlist private).
        track_uris: Optional list of track IDs/URIs/URLs to add on creation.
    """
    client = _get_client()
    playlist = client.create_playlist(
        name=name,
        public=public,
        description=description,
        collaborative=collaborative,
    )
    if track_uris:
        result = client.add_tracks(playlist["id"], track_uris)
        playlist["tracks_added"] = result.get("added")
        playlist["snapshot_id"] = result.get("snapshot_id")
    return playlist


@mcp.tool()
@_guard
def add_tracks_to_playlist(
    playlist_id: str, track_uris: list[str], position: Optional[int] = None
) -> dict:
    """Add one or more songs to an existing playlist.

    Args:
        playlist_id: A Spotify playlist ID, URI, or URL.
        track_uris: List of track IDs, `spotify:track:...` URIs, or track URLs.
        position: Optional zero-based index to insert at (defaults to the end).
    """
    return _get_client().add_tracks(playlist_id, track_uris, position=position)


# --------------------------------------------------------------------------
# Playback (read-only convenience)
# --------------------------------------------------------------------------


@mcp.tool()
@_guard
def get_currently_playing() -> dict:
    """Show the track currently playing on the user's Spotify account (if any)."""
    return _get_client().currently_playing()


def run() -> None:
    """Entry point for running the server over stdio."""
    mcp.run()
