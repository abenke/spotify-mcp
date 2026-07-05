"""Audio-feature attributes (tempo/BPM, key, energy, danceability, …).

Spotify deprecated its own ``/v1/audio-features`` and ``/v1/audio-analysis``
endpoints on 2024-11-27; apps created after that date receive HTTP 403 and there
is no official replacement. This module uses ReccoBeats
(https://reccobeats.com), a free third-party service that mirrors the same
metrics and accepts Spotify track IDs, so the data is ReccoBeats' estimate
rather than Spotify's original numbers.

Flow (per ReccoBeats' resource model):
  1. Resolve Spotify track IDs to ReccoBeats IDs via GET /v1/track?ids=...
  2. Fetch features per track via GET /v1/track/{reccobeats_id}/audio-features
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

DEFAULT_BASE_URL = "https://api.reccobeats.com"

# Pitch-class integer -> name, matching the convention Spotify used for `key`.
_PITCH_CLASSES = [
    "C", "C♯/D♭", "D", "D♯/E♭", "E", "F",
    "F♯/G♭", "G", "G♯/A♭", "A", "A♯/B♭", "B",
]


class AudioFeaturesError(RuntimeError):
    """Raised when the audio-features provider returns an error."""


class AudioFeaturesClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (
            base_url or os.environ.get("RECCOBEATS_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._client = httpx.Client(timeout=30, headers={"Accept": "application/json"})

    def _get(self, path: str, **kwargs) -> dict:
        try:
            resp = self._client.get(f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise AudioFeaturesError(
                f"Could not reach the audio-features provider ({self.base_url}): {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise AudioFeaturesError(
                f"Audio-features provider error {resp.status_code} for {path}: "
                f"{resp.text[:200]}"
            )
        if not resp.content:
            return {}
        return resp.json()

    def _resolve_ids(self, spotify_ids: list[str]) -> dict[str, str]:
        """Map Spotify track IDs -> ReccoBeats IDs."""
        data = self._get("/v1/track", params={"ids": ",".join(spotify_ids)})
        items = data.get("content") if isinstance(data, dict) else data
        mapping: dict[str, str] = {}
        for item in items or []:
            recco_id = item.get("id")
            spotify_id = item.get("spotifyId")
            if not spotify_id:
                href = item.get("href") or ""
                if "/track/" in href:
                    spotify_id = href.split("/track/")[1].split("?")[0].split("/")[0]
            if recco_id and spotify_id:
                mapping[spotify_id] = recco_id
        return mapping

    def _features_for(self, recco_id: str) -> dict:
        return self._get(f"/v1/track/{recco_id}/audio-features")

    def get_audio_features(self, spotify_ids: list[str]) -> list[dict]:
        mapping = self._resolve_ids(spotify_ids)
        results: list[dict] = []
        for spotify_id in spotify_ids:
            recco_id = mapping.get(spotify_id)
            if not recco_id:
                results.append(
                    {
                        "spotify_id": spotify_id,
                        "error": "Track not found in the ReccoBeats database.",
                    }
                )
                continue
            results.append(_shape_features(spotify_id, self._features_for(recco_id)))
        return results

    def close(self) -> None:
        self._client.close()


def _shape_features(spotify_id: str, feats: dict) -> dict:
    """Pass through the metric fields and add human-friendly derivations."""
    out: dict = {"spotify_id": spotify_id}
    for key, value in (feats or {}).items():
        if key in ("id", "href"):
            continue
        out[key] = value

    tempo = out.get("tempo")
    if isinstance(tempo, (int, float)):
        out["bpm"] = round(tempo)

    key = out.get("key")
    if isinstance(key, int) and 0 <= key < 12:
        name = _PITCH_CLASSES[key]
        mode = out.get("mode")
        if mode == 1:
            name += " major"
        elif mode == 0:
            name += " minor"
        out["key_name"] = name

    return out
