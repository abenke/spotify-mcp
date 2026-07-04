"""A thin client over the Spotify Web API.

Handles auth headers, error surfacing, pagination helpers, and trims Spotify's
verbose JSON down to the fields that are useful in an assistant context.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .auth import SpotifyAuth

API_BASE = "https://api.spotify.com/v1"


class SpotifyAPIError(RuntimeError):
    """Raised when the Spotify Web API returns an error response."""


class SpotifyClient:
    def __init__(self, auth: SpotifyAuth):
        self.auth = auth
        self._client = httpx.Client(base_url=API_BASE, timeout=30)
        self._me: Optional[dict] = None

    # ---- low-level request -------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        token = self.auth.get_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        resp = self._client.request(method, path, headers=headers, **kwargs)

        if resp.status_code == 401:
            # Token may have just expired mid-flight; force a refresh once.
            token = self.auth.get_access_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = self._client.request(method, path, headers=headers, **kwargs)

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "a few")
            raise SpotifyAPIError(
                f"Rate limited by Spotify. Retry after {retry_after} seconds."
            )
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("error", {}).get("message", detail)
            except Exception:
                pass
            raise SpotifyAPIError(f"Spotify API error {resp.status_code}: {detail}")

        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # ---- profile -----------------------------------------------------------

    def current_user(self) -> dict:
        if self._me is None:
            self._me = self._request("GET", "/me")
        return self._me

    def current_user_summary(self) -> dict:
        me = self.current_user()
        return {
            "id": me.get("id"),
            "display_name": me.get("display_name"),
            "email": me.get("email"),
            "product": me.get("product"),
            "country": me.get("country"),
            "followers": (me.get("followers") or {}).get("total"),
            "external_url": (me.get("external_urls") or {}).get("spotify"),
        }

    # ---- search ------------------------------------------------------------

    def search(self, query: str, types: list[str], limit: int = 10) -> dict:
        params = {
            "q": query,
            "type": ",".join(types),
            "limit": max(1, min(limit, 50)),
        }
        data = self._request("GET", "/search", params=params)
        out: dict[str, Any] = {}
        if "tracks" in data:
            out["tracks"] = [_track_summary(t) for t in data["tracks"]["items"] if t]
        if "artists" in data:
            out["artists"] = [_artist_summary(a) for a in data["artists"]["items"] if a]
        if "albums" in data:
            out["albums"] = [_album_summary(a) for a in data["albums"]["items"] if a]
        if "playlists" in data:
            out["playlists"] = [
                _playlist_summary(p) for p in data["playlists"]["items"] if p
            ]
        return out

    # ---- tracks ------------------------------------------------------------

    def get_track(self, track_id: str) -> dict:
        track_id = _extract_id(track_id, "track")
        data = self._request("GET", f"/tracks/{track_id}")
        return _track_detail(data)

    # ---- playlists ---------------------------------------------------------

    def list_my_playlists(self, limit: int = 20, offset: int = 0) -> dict:
        params = {"limit": max(1, min(limit, 50)), "offset": max(0, offset)}
        data = self._request("GET", "/me/playlists", params=params)
        return {
            "total": data.get("total"),
            "offset": data.get("offset"),
            "limit": data.get("limit"),
            "next_offset": (
                data.get("offset", 0) + data.get("limit", 0)
                if data.get("next")
                else None
            ),
            "playlists": [_playlist_summary(p) for p in data.get("items", []) if p],
        }

    def get_playlist(self, playlist_id: str, tracks_limit: int = 100) -> dict:
        playlist_id = _extract_id(playlist_id, "playlist")
        data = self._request("GET", f"/playlists/{playlist_id}")
        summary = _playlist_summary(data)

        items = (data.get("tracks") or {}).get("items", [])
        tracks = []
        for item in items[:tracks_limit]:
            track = item.get("track")
            if track and track.get("type") == "track":
                tracks.append(_track_summary(track))
        summary["tracks_total"] = (data.get("tracks") or {}).get("total")
        summary["tracks"] = tracks
        summary["tracks_truncated"] = (
            (data.get("tracks") or {}).get("total", 0) > len(tracks)
        )
        return summary

    def create_playlist(
        self,
        name: str,
        public: bool = False,
        description: str = "",
        collaborative: bool = False,
    ) -> dict:
        user_id = self.current_user()["id"]
        body = {
            "name": name,
            "public": public,
            "collaborative": collaborative,
            "description": description,
        }
        # Collaborative playlists must be private per Spotify's rules.
        if collaborative:
            body["public"] = False
        data = self._request("POST", f"/users/{user_id}/playlists", json=body)
        return _playlist_summary(data)

    def add_tracks(
        self, playlist_id: str, track_uris: list[str], position: Optional[int] = None
    ) -> dict:
        playlist_id = _extract_id(playlist_id, "playlist")
        uris = [_to_track_uri(u) for u in track_uris]
        if not uris:
            raise SpotifyAPIError("No track URIs/IDs provided to add.")
        # Spotify accepts at most 100 items per request.
        snapshot = None
        added = 0
        for chunk_start in range(0, len(uris), 100):
            chunk = uris[chunk_start : chunk_start + 100]
            body: dict[str, Any] = {"uris": chunk}
            if position is not None and chunk_start == 0:
                body["position"] = position
            data = self._request(
                "POST", f"/playlists/{playlist_id}/tracks", json=body
            )
            snapshot = data.get("snapshot_id", snapshot)
            added += len(chunk)
        return {"added": added, "snapshot_id": snapshot, "playlist_id": playlist_id}

    # ---- playback (read-only convenience) ----------------------------------

    def currently_playing(self) -> dict:
        data = self._request("GET", "/me/player/currently-playing")
        if not data or not data.get("item"):
            return {"is_playing": False, "message": "Nothing is currently playing."}
        item = data["item"]
        return {
            "is_playing": data.get("is_playing"),
            "progress_ms": data.get("progress_ms"),
            "track": _track_summary(item) if item.get("type") == "track" else None,
        }

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------
# Response trimming helpers
# --------------------------------------------------------------------------


def _artist_names(obj: dict) -> list[str]:
    return [a.get("name") for a in obj.get("artists", []) if a.get("name")]


def _track_summary(t: dict) -> dict:
    album = t.get("album") or {}
    return {
        "id": t.get("id"),
        "name": t.get("name"),
        "uri": t.get("uri"),
        "artists": _artist_names(t),
        "album": album.get("name"),
        "duration_ms": t.get("duration_ms"),
        "explicit": t.get("explicit"),
        "external_url": (t.get("external_urls") or {}).get("spotify"),
    }


def _track_detail(t: dict) -> dict:
    album = t.get("album") or {}
    summary = _track_summary(t)
    summary.update(
        {
            "popularity": t.get("popularity"),
            "track_number": t.get("track_number"),
            "disc_number": t.get("disc_number"),
            "album_release_date": album.get("release_date"),
            "album_id": album.get("id"),
            "isrc": (t.get("external_ids") or {}).get("isrc"),
            "preview_url": t.get("preview_url"),
        }
    )
    return summary


def _artist_summary(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "uri": a.get("uri"),
        "genres": a.get("genres"),
        "followers": (a.get("followers") or {}).get("total"),
        "popularity": a.get("popularity"),
        "external_url": (a.get("external_urls") or {}).get("spotify"),
    }


def _album_summary(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "uri": a.get("uri"),
        "artists": _artist_names(a),
        "release_date": a.get("release_date"),
        "total_tracks": a.get("total_tracks"),
        "external_url": (a.get("external_urls") or {}).get("spotify"),
    }


def _playlist_summary(p: dict) -> dict:
    owner = p.get("owner") or {}
    tracks = p.get("tracks") or {}
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "uri": p.get("uri"),
        "description": p.get("description"),
        "owner": owner.get("display_name") or owner.get("id"),
        "public": p.get("public"),
        "collaborative": p.get("collaborative"),
        "tracks_total": tracks.get("total"),
        "external_url": (p.get("external_urls") or {}).get("spotify"),
    }


# --------------------------------------------------------------------------
# ID / URI parsing helpers
# --------------------------------------------------------------------------


def _extract_id(value: str, kind: str) -> str:
    """Accept a raw ID, a spotify: URI, or an open.spotify.com URL."""
    value = value.strip()
    if value.startswith(f"spotify:{kind}:"):
        return value.split(":")[-1]
    if "open.spotify.com" in value:
        # e.g. https://open.spotify.com/playlist/<id>?si=...
        part = value.split(f"/{kind}/", 1)
        if len(part) == 2:
            return part[1].split("?")[0].split("/")[0]
    return value


def _to_track_uri(value: str) -> str:
    value = value.strip()
    if value.startswith("spotify:track:"):
        return value
    track_id = _extract_id(value, "track")
    return f"spotify:track:{track_id}"
