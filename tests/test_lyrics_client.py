"""Integration test: LyricsClient against a local stub of the LRCLIB API."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from spotify_mcp.structure import LyricsClient, analyze_structure, parse_lrc

_SYNCED = "\n".join(
    [
        "[00:05.00]Verse line about the morning",
        "[00:09.00]Another verse line entirely",
        "[00:13.00]A third verse line of words",
        "[00:17.00]Closing out the opening verse",
        "[00:21.00]This is the chorus hook",
        "[00:24.50]Singing it loud together",
        "[00:28.00]This is where we all belong",
        "[00:35.00]Second verse says something new",
        "[00:39.00]Completely different words now",
        "[00:43.00]Nothing like the lines before",
        "[00:47.00]Winding up towards the drop",
        "[00:51.00]This is the chorus hook",
        "[00:54.50]Singing it loud together",
        "[00:58.00]This is where we all belong",
        "[01:05.00]Quiet outro fading away",
    ]
)

_RECORD = {
    "id": 123,
    "trackName": "Stub Song",
    "artistName": "Stub Artist",
    "albumName": "Stub Album",
    "duration": 70.0,
    "instrumental": False,
    "plainLyrics": "irrelevant",
    "syncedLyrics": _SYNCED,
}


class _StubLrclib(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/get" and params.get("track_name") == "Stub Song":
            body = json.dumps(_RECORD).encode()
            self.send_response(200)
        elif url.path == "/api/search":
            body = json.dumps([_RECORD]).encode()
            self.send_response(200)
        else:
            body = json.dumps({"code": 404, "message": "Not found"}).encode()
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def stub_server():
    server = HTTPServer(("127.0.0.1", 0), _StubLrclib)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_fetch_by_signature_then_analyze(stub_server):
    client = LyricsClient(base_url=stub_server)
    record = client.fetch_lyrics("Stub Artist", "Stub Song", duration_s=70.0)
    assert record["trackName"] == "Stub Song"

    result = analyze_structure(
        parse_lrc(record["syncedLyrics"]), track_duration_s=record["duration"]
    )
    chorus = result["sections"][0]
    assert chorus["label"] == "chorus"
    assert chorus["occurrence_count"] == 2
    # Chorus 1 runs 0:21 -> 0:35 (14 s); chorus 2 runs 0:51 -> 1:05 (14 s).
    assert chorus["durations_seconds"] == [14, 14]
    client.close()


def test_fetch_falls_back_to_search(stub_server):
    client = LyricsClient(base_url=stub_server)
    record = client.fetch_lyrics("Stub Artist", "Slightly Wrong Title")
    assert record["trackName"] == "Stub Song"
    client.close()
