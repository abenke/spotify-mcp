"""Song-structure analysis: find choruses and other repeated sections.

Spotify serves no raw audio, and its ``/v1/audio-analysis`` endpoint (the one
that exposed section timings) was deprecated on 2024-11-27, so structure can't
come from Spotify itself. This module instead uses time-synced lyrics from
LRCLIB (https://lrclib.net), a free community database that needs no API key.

The idea: a chorus is a block of lyric lines that repeats (near-)verbatim at
several points in the song. Synced lyrics give a timestamp per line, so once
the repeated blocks are found, each occurrence's start, end, and duration fall
out of the timestamps — e.g. a chorus sung three times for 14 seconds each
reports durations [14, 14, 14].

Detection pipeline (pure functions, no network):
  1. Parse the LRC text into (timestamp, line) pairs.
  2. Normalize lines and compare them fuzzily (small ad-lib variations still
     count as the same line).
  3. Find maximal runs of consecutive matching lines between every pair of
     positions in the song — the diagonals of a line-level self-similarity
     matrix. Each run means "these two stretches repeat each other".
  4. Cluster the stretches into families of occurrences of the same section.
  5. Rank families by total repeated time: the top family is the chorus,
     the rest are sub-choruses (pre-/post-chorus hooks, repeated bridges).
"""

from __future__ import annotations

import difflib
import os
import re
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "https://lrclib.net"
# LRCLIB asks clients to identify themselves with a descriptive User-Agent.
_USER_AGENT = "spotify-mcp/0.1.0 (https://github.com/abenke/spotify-mcp)"


class StructureError(RuntimeError):
    """Raised when lyrics can't be fetched or analyzed."""


# --------------------------------------------------------------------------
# LRCLIB client
# --------------------------------------------------------------------------


class LyricsClient:
    """Fetches synced lyrics for a track from LRCLIB."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (
            base_url or os.environ.get("LRCLIB_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._client = httpx.Client(
            timeout=30,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )

    def _get(self, path: str, params: dict) -> Any:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        try:
            resp = self._client.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise StructureError(
                f"Could not reach the lyrics provider ({self.base_url}): {exc}"
            ) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise StructureError(
                f"Lyrics provider error {resp.status_code} for {path}: "
                f"{resp.text[:200]}"
            )
        return resp.json()

    def fetch_lyrics(
        self,
        artist: str,
        title: str,
        album: Optional[str] = None,
        duration_s: Optional[float] = None,
    ) -> Optional[dict]:
        """Best-match lyrics record for a track, or None if LRCLIB has nothing.

        Tries the exact-signature endpoint first, then falls back to search.
        """
        record = self._get(
            "/api/get",
            {
                "artist_name": artist,
                "track_name": title,
                "album_name": album,
                "duration": round(duration_s) if duration_s else None,
            },
        )
        if record:
            return record

        results = self._get(
            "/api/search", {"track_name": title, "artist_name": artist}
        )
        if not results:
            results = self._get("/api/search", {"q": f"{artist} {title}"})
        if not results:
            return None

        def rank(rec: dict) -> tuple:
            has_synced = bool(rec.get("syncedLyrics"))
            if duration_s and rec.get("duration"):
                closeness = -abs(rec["duration"] - duration_s)
            else:
                closeness = 0.0
            return (has_synced, closeness)

        return max(results, key=rank)

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------
# LRC parsing
# --------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")
# Enhanced-LRC per-word tags like <01:23.45>, occasionally present.
_WORD_TAG_RE = re.compile(r"<\d+:\d{1,2}(?:\.\d{1,3})?>")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Parse LRC text into (seconds, line) pairs sorted by time.

    Lines with no ``[mm:ss.xx]`` timestamp (metadata tags like ``[ar:...]``)
    are skipped. A line may carry several timestamps; it's emitted once per
    timestamp. Empty timestamped lines are kept — they mark instrumental gaps
    and serve as section boundaries.
    """
    out: list[tuple[float, str]] = []
    for raw in text.splitlines():
        stamps = list(_TIMESTAMP_RE.finditer(raw))
        if not stamps:
            continue
        content = raw[stamps[-1].end() :]
        content = " ".join(_WORD_TAG_RE.sub(" ", content).split())
        for m in stamps:
            minutes, seconds = m.group(1), m.group(2).replace(":", ".")
            out.append((int(minutes) * 60 + float(seconds), content))
    out.sort(key=lambda pair: pair[0])
    return out


# --------------------------------------------------------------------------
# Repeated-section detection
# --------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s']", flags=re.UNICODE)


def _normalize(text: str) -> str:
    return " ".join(_PUNCT_RE.sub(" ", text.lower()).split())


class _LineMatcher:
    """Fuzzy equality between lyric lines, with caching (choruses repeat)."""

    def __init__(self, texts: list[str], threshold: float):
        self.texts = texts
        self.threshold = threshold
        self._cache: dict[tuple[str, str], bool] = {}

    def match(self, i: int, j: int) -> bool:
        a, b = self.texts[i], self.texts[j]
        if not a or not b:
            return False
        if a == b:
            return True
        key = (a, b) if a <= b else (b, a)
        cached = self._cache.get(key)
        if cached is None:
            sm = difflib.SequenceMatcher(None, a, b)
            cached = (
                sm.real_quick_ratio() >= self.threshold
                and sm.quick_ratio() >= self.threshold
                and sm.ratio() >= self.threshold
            )
            self._cache[key] = cached
        return cached


def _find_repeat_runs(
    matcher: _LineMatcher, n: int, min_lines: int
) -> list[tuple[int, int, int]]:
    """Maximal diagonal runs (i, j, length): lines[i:i+L] repeats at lines[j:j+L].

    Run length is capped at the lag ``j - i`` so the two copies never overlap
    (this also discards a single line chanted back-to-back within one section).
    """
    runs: list[tuple[int, int, int]] = []
    for lag in range(1, n):
        i = 0
        while i + lag < n:
            if not matcher.match(i, i + lag):
                i += 1
                continue
            length = 1
            while i + length + lag < n and matcher.match(i + length, i + length + lag):
                length += 1
            effective = min(length, lag)
            if effective >= min_lines:
                runs.append((i, i + lag, effective))
            i += length
    return runs


def _split_runs(
    runs: list[tuple[int, int, int]], min_lines: int
) -> list[tuple[int, int, int]]:
    """Split runs at section boundaries supported by other runs.

    When a chorus is sometimes followed by a post-chorus hook and sometimes
    not, some runs span "chorus + hook" while others span just the chorus.
    The shorter runs' endpoints mark a boundary inside the longer runs; cut
    the longer runs there so chorus and hook cluster as separate sections.
    A cut needs two supporting endpoints so a single stray match can't
    fragment a real section.
    """
    endpoint_counts: dict[int, int] = {}
    for i, j, length in runs:
        for point in (i, i + length, j, j + length):
            endpoint_counts[point] = endpoint_counts.get(point, 0) + 1

    split: list[tuple[int, int, int]] = []
    for i, j, length in runs:
        cuts = [
            off
            for off in range(1, length)
            if endpoint_counts.get(i + off, 0) + endpoint_counts.get(j + off, 0) >= 2
        ]
        prev = 0
        for off in cuts + [length]:
            if off - prev >= min_lines:
                split.append((i + prev, j + prev, off - prev))
            prev = off
    return split


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _cluster_families(
    runs: list[tuple[int, int, int]]
) -> list[list[tuple[int, int]]]:
    """Group repeated stretches into families of occurrences of one section.

    Stretches are linked when a run pairs them (same content at two places) or
    when they substantially overlap in position (same place found twice).
    Overlapping members of a family are then merged into clean occurrences.
    """
    intervals: list[tuple[int, int]] = []  # [start, end) line-index ranges
    pairs: list[tuple[int, int]] = []  # indexes into `intervals` to union
    for i, j, length in runs:
        intervals.append((i, i + length))
        intervals.append((j, j + length))
        pairs.append((len(intervals) - 2, len(intervals) - 1))

    uf = _UnionFind(len(intervals))
    for a, b in pairs:
        uf.union(a, b)
    for a in range(len(intervals)):
        for b in range(a + 1, len(intervals)):
            sa, ea = intervals[a]
            sb, eb = intervals[b]
            overlap = min(ea, eb) - max(sa, sb)
            if overlap > 0 and overlap >= 0.5 * min(ea - sa, eb - sb):
                uf.union(a, b)

    groups: dict[int, list[tuple[int, int]]] = {}
    for idx, interval in enumerate(intervals):
        groups.setdefault(uf.find(idx), []).append(interval)

    families = []
    for members in groups.values():
        members.sort()
        merged: list[list[int]] = []
        for start, end in members:
            if merged and start < merged[-1][1]:  # strict overlap only
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        families.append([(s, e) for s, e in merged])
    return families


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _occurrence(
    start: int,
    end: int,
    times: Optional[list[float]],
    end_of_song_s: Optional[float],
) -> dict:
    occ: dict[str, Any] = {"first_line": start, "last_line": end - 1}
    if times is None:
        return occ
    start_s = times[start]
    if end < len(times):
        end_s = times[end]
    elif end_of_song_s is not None and end_of_song_s > start_s:
        end_s = end_of_song_s
    else:
        # No line after the block and no track duration: extrapolate from the
        # song's median gap between consecutive lines.
        gaps = sorted(
            b - a for a, b in zip(times, times[1:]) if b > a
        )
        median_gap = gaps[len(gaps) // 2] if gaps else 3.0
        end_s = times[end - 1] + median_gap
    occ.update(
        {
            "start": _fmt_time(start_s),
            "end": _fmt_time(end_s),
            "start_seconds": round(start_s, 1),
            "end_seconds": round(end_s, 1),
            "duration_seconds": int(round(end_s - start_s)),
        }
    )
    return occ


def analyze_structure(
    lines: list[tuple[Optional[float], str]],
    track_duration_s: Optional[float] = None,
    similarity: float = 0.85,
    min_lines: int = 2,
    min_seconds: float = 6.0,
    max_sections: int = 4,
) -> dict:
    """Find the chorus and other repeated sections in a lyric timeline.

    Args:
        lines: (timestamp_seconds, text) pairs in time order. Timestamps may
            all be None (plain lyrics) — sections are still found and counted,
            but without durations.
        track_duration_s: Total track length, used to time a section that ends
            the song.
        similarity: Fuzzy line-match threshold (0-1).
        min_lines: Minimum lines for a repeated block to count.
        min_seconds: Minimum duration for a repeated block to count.
        max_sections: Cap on how many section families to report.
    """
    texts = [_normalize(text) for _, text in lines]
    synced = bool(lines) and lines[0][0] is not None
    times = [t for t, _ in lines] if synced else None

    matcher = _LineMatcher(texts, similarity)
    runs = _find_repeat_runs(matcher, len(lines), min_lines)
    runs = _split_runs(runs, min_lines)
    families = _cluster_families(runs)

    candidates = []
    for occurrences in families:
        occs = [
            _occurrence(start, end, times, track_duration_s)
            for start, end in occurrences
        ]
        if synced:
            occs = [o for o in occs if o["duration_seconds"] >= min_seconds]
        if len(occs) < 2:
            continue
        score = (
            sum(o["duration_seconds"] for o in occs)
            if synced
            else sum(o["last_line"] - o["first_line"] + 1 for o in occs)
        )
        candidates.append({"score": score, "occurrences": occs})
    candidates.sort(key=lambda c: (c["score"], len(c["occurrences"])), reverse=True)

    # Drop families that mostly live inside a higher-ranked family (e.g. a
    # hook line that only ever appears inside the chorus).
    kept: list[dict] = []
    covered: set[int] = set()
    for cand in candidates:
        cand_lines: set[int] = set()
        for o in cand["occurrences"]:
            cand_lines.update(range(o["first_line"], o["last_line"] + 1))
        if cand_lines and len(cand_lines & covered) / len(cand_lines) >= 0.75:
            continue
        kept.append(cand)
        covered |= cand_lines
        if len(kept) >= max_sections:
            break

    sections = []
    for rank, cand in enumerate(kept):
        occs = cand["occurrences"]
        label = "chorus" if rank == 0 else f"sub-chorus {rank}"
        first = occs[0]
        sample = [
            lines[i][1]
            for i in range(first["first_line"], min(first["last_line"] + 1, first["first_line"] + 3))
            if lines[i][1]
        ]
        section: dict[str, Any] = {
            "label": label,
            "occurrence_count": len(occs),
            "sample_lines": sample,
            "occurrences": occs,
        }
        if synced:
            durations = [o["duration_seconds"] for o in occs]
            section["durations_seconds"] = durations
            section["gaps_between_seconds"] = [
                int(round(nxt["start_seconds"] - cur["end_seconds"]))
                for cur, nxt in zip(occs, occs[1:])
            ]
            section["summary"] = (
                f"{label}: {len(occs)} times, "
                f"{', '.join(f'{d}s' for d in durations)}"
            )
        else:
            section["summary"] = f"{label}: {len(occs)} times (no timing available)"
        sections.append(section)

    result: dict[str, Any] = {
        "synced": synced,
        "line_count": len(lines),
        "sections": sections,
    }
    if synced:
        result["timeline"] = _build_timeline(sections, track_duration_s)
    return result


def _build_timeline(
    sections: list[dict], track_duration_s: Optional[float]
) -> list[dict]:
    """Time-ordered song map: detected sections with the gaps labeled."""
    placed: list[tuple[float, float, str]] = []
    for section in sections:  # already ranked; earlier sections win overlaps
        for occ in section["occurrences"]:
            start, end = occ["start_seconds"], occ["end_seconds"]
            if all(end <= s or start >= e for s, e, _ in placed):
                placed.append((start, end, section["label"]))
    placed.sort()

    timeline: list[dict] = []

    def add(start: float, end: float, label: str) -> None:
        timeline.append(
            {
                "start": _fmt_time(start),
                "end": _fmt_time(end),
                "duration_seconds": int(round(end - start)),
                "label": label,
            }
        )

    cursor = 0.0
    for start, end, label in placed:
        if start - cursor >= 4:
            add(cursor, start, "other (intro/verse)" if not timeline else "other (verse/bridge)")
        add(start, end, label)
        cursor = end
    if track_duration_s and track_duration_s - cursor >= 4:
        add(cursor, track_duration_s, "other (outro)")
    return timeline
