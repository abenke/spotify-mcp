"""The Spotify MCP server.

Exposes tools for authenticating, searching Spotify's catalog, viewing tracks
and playlists, and creating/populating playlists.
"""

from __future__ import annotations

import functools
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .audio_features import AudioFeaturesClient, AudioFeaturesError
from .auth import SpotifyAuth, SpotifyAuthError
from .client import SpotifyAPIError, SpotifyClient, _extract_id
from .structure import LyricsClient, StructureError, analyze_structure, parse_lrc

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
_audio_client: Optional[AudioFeaturesClient] = None
_lyrics_client: Optional[LyricsClient] = None


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


def _get_audio_client() -> AudioFeaturesClient:
    global _audio_client
    if _audio_client is None:
        _audio_client = AudioFeaturesClient()
    return _audio_client


def _get_lyrics_client() -> LyricsClient:
    global _lyrics_client
    if _lyrics_client is None:
        _lyrics_client = LyricsClient()
    return _lyrics_client


def _guard(fn):
    """Convert internal exceptions into clean, model-readable error strings.

    ``functools.wraps`` sets ``__wrapped__`` so FastMCP's signature
    introspection sees the real parameters rather than ``*args, **kwargs``.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (
            SpotifyAuthError,
            SpotifyAPIError,
            AudioFeaturesError,
            StructureError,
        ) as exc:
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


@mcp.tool()
@_guard
def get_audio_features(track_ids: list[str]) -> dict:
    """Get audio-analysis attributes for songs: tempo/BPM, key, energy,
    danceability, valence, acousticness, loudness, and more.

    Note: Spotify deprecated its own audio-features endpoint for apps created
    after 2024-11-27 (it returns 403). This tool instead uses ReccoBeats, a free
    third-party service that mirrors the same metrics and accepts Spotify track
    IDs. The track IDs are sent to ReccoBeats and the returned values are its
    estimates, not Spotify's original numbers.

    Args:
        track_ids: One or more Spotify track IDs, `spotify:track:...` URIs, or
            open.spotify.com track URLs.
    """
    if isinstance(track_ids, str):
        track_ids = [track_ids]
    ids = [_extract_id(t, "track") for t in track_ids if t and t.strip()]
    if not ids:
        return {"error": "No track IDs provided."}
    return {"audio_features": _get_audio_client().get_audio_features(ids)}


@mcp.tool()
@_guard
def find_choruses(
    track_id: Optional[str] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
) -> dict:
    """Find a song's choruses and other repeated sections, with the length in
    seconds of every occurrence — e.g. a chorus sung three times for 14 seconds
    each reports durations [14, 14, 14]. Also reports when each occurrence
    starts/ends, the gaps between them, and a full song timeline. Useful for
    planning workouts, choreography, or class progressions around a song.

    Identify the song either by Spotify track (requires authentication) or by
    artist + title directly (no authentication needed).

    How it works: choruses are detected as blocks of lyric lines that repeat at
    several timestamps in the song, using time-synced lyrics from LRCLIB — a
    free community lyrics database (the artist/title you look up is sent to
    lrclib.net). Instrumental tracks, and tracks LRCLIB doesn't know, can't be
    analyzed this way.

    Args:
        track_id: A Spotify track ID, `spotify:track:...` URI, or
            open.spotify.com track URL.
        artist: Artist name (used with `title` when no track_id is given).
        title: Song title (used with `artist` when no track_id is given).
    """
    album = None
    duration_s = None
    track_info: dict = {}
    if track_id:
        track = _get_client().get_track(track_id)
        title = track.get("name")
        artists = track.get("artists") or []
        artist = artists[0] if artists else None
        album = track.get("album")
        if track.get("duration_ms"):
            duration_s = track["duration_ms"] / 1000
        track_info = {
            "id": track.get("id"),
            "name": title,
            "artists": artists,
            "duration_seconds": round(duration_s) if duration_s else None,
        }
    if not artist or not title:
        return {
            "error": "Provide either a Spotify track_id, or both artist and title."
        }

    record = _get_lyrics_client().fetch_lyrics(
        artist, title, album=album, duration_s=duration_s
    )
    if not record:
        return {
            "error": f"No lyrics found for '{title}' by {artist} on LRCLIB, "
            "so the song's structure can't be analyzed."
        }
    if record.get("instrumental"):
        return {
            "error": f"'{title}' by {artist} is marked instrumental on LRCLIB "
            "(no lyrics), so choruses can't be detected from lyrics."
        }

    lyrics_info = {
        "matched_title": record.get("trackName"),
        "matched_artist": record.get("artistName"),
        "source": "lrclib.net",
    }
    if duration_s is None and record.get("duration"):
        duration_s = record["duration"]

    synced = record.get("syncedLyrics")
    if synced:
        lines = parse_lrc(synced)
        result = analyze_structure(lines, track_duration_s=duration_s)
    else:
        plain = record.get("plainLyrics") or ""
        lines = [(None, line.strip()) for line in plain.splitlines()]
        if not any(text for _, text in lines):
            return {
                "error": f"LRCLIB has no usable lyrics for '{title}' by {artist}."
            }
        result = analyze_structure(lines)
        result["note"] = (
            "Only un-synced lyrics were available, so sections are counted "
            "but their durations in seconds can't be measured."
        )

    if not result["sections"]:
        result["note"] = (
            "No repeated lyric sections were detected — the song may have "
            "through-composed lyrics or the synced lyrics may be incomplete."
        )
    if track_info:
        result["track"] = track_info
    result["lyrics"] = lyrics_info
    return result


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
