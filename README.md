# Spotify MCP

A local [Model Context Protocol](https://modelcontextprotocol.io) server for the
[Spotify Web API](https://developer.spotify.com/documentation/web-api). Runs on
your machine and lets an MCP client — **Claude Code**, the **Coworker** desktop
app, or anything else that speaks MCP — search Spotify, view song and playlist
details, list your playlists, and create new ones on your behalf.

## Features

| Tool | What it does |
| --- | --- |
| `authenticate` | Sign in to Spotify via OAuth (opens a browser). Run once. |
| `auth_status` | Check whether the server is authenticated and how it's configured. |
| `get_current_user` | Your Spotify profile (id, display name, country, …). |
| `search` | Search the catalog for tracks, albums, artists, or playlists. |
| `get_track` | Detailed info for a single song. |
| `get_audio_features` | Tempo/BPM, key, energy, danceability, etc. for songs (see note below). |
| `find_choruses` | Detect a song's choruses & repeated sections, with the length in seconds of each occurrence (see note below). |
| `list_my_playlists` | List the playlists you own or follow (paginated). |
| `get_playlist` | A playlist's details and its tracks. |
| `create_playlist` | Create a new playlist (optionally seeded with tracks). |
| `add_tracks_to_playlist` | Add songs to an existing playlist. |
| `get_currently_playing` | The track currently playing on your account, if any. |

## How it works

Authentication uses the **Authorization Code with PKCE** flow — Spotify's
recommended flow for locally-run apps, because it needs only a Client ID and no
client secret. On first use the server opens your browser, you approve access,
and the resulting tokens are cached at `~/.spotify-mcp/token.json` and refreshed
automatically. You typically authenticate just once per machine.

## Audio features (tempo/BPM, key, energy, danceability)

Spotify [deprecated](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api)
its own `audio-features` and `audio-analysis` endpoints on 2024-11-27. Apps
created after that date get `403 Forbidden`, and there is no official
replacement.

So `get_audio_features` instead uses [ReccoBeats](https://reccobeats.com) — a
free third-party service that mirrors the same metrics and accepts Spotify track
IDs. Two things to be aware of:

- Track IDs you look up are sent to ReccoBeats (not Spotify).
- The returned values are ReccoBeats' estimates, not Spotify's original numbers.

No API key is required. You can point the tool at a different base URL with the
`RECCOBEATS_BASE_URL` environment variable.

## Chorus detection (`find_choruses`)

Built for planning workouts, choreography, or class progressions around a
song's structure: `find_choruses` reports every time the chorus (and any other
repeated section, like a post-chorus hook) occurs, how long each occurrence
runs in seconds — a chorus sung three times for 14 seconds each comes back as
`[14, 14, 14]` — the gaps between occurrences, and a start-to-finish timeline
of the song.

Since Spotify serves no raw audio and deprecated its `audio-analysis` endpoint
(the one that exposed section timings), the analysis works from **time-synced
lyrics** instead: a chorus is a block of lyric lines that repeats
(near-)verbatim at several timestamps, and the line timestamps yield each
occurrence's start, end, and duration. Lyrics come from
[LRCLIB](https://lrclib.net), a free community lyrics database that needs no
API key.

Things to be aware of:

- You can identify the song by Spotify track ID **or** by artist + title —
  the latter needs no Spotify authentication at all.
- The artist/title you look up is sent to LRCLIB (not Spotify). Point the tool
  at a different base URL with the `LRCLIB_BASE_URL` environment variable.
- Instrumental tracks, and tracks LRCLIB doesn't have synced lyrics for, can't
  be analyzed. If only un-synced lyrics exist, sections are counted but not
  timed.
- Durations run from the first line of a section to the first line of the next
  one, so a chorus's trailing instrumental bars count toward the chorus.
- If a hook *always* follows the chorus, the lyrics give no evidence they are
  separate sections, and they'll be reported as one combined block.

## Prerequisites

- Python 3.10+
- A free Spotify account
- A Spotify app (for the Client ID):
  1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
     and **Create app**.
  2. Under **Redirect URIs**, add exactly:
     ```
     http://127.0.0.1:8888/callback
     ```
     > Spotify requires loopback redirect URIs to use `127.0.0.1` (not
     > `localhost`). If you change the port, update `SPOTIFY_REDIRECT_URI` to match.
  3. Copy the **Client ID**. (The Client Secret is **not** required.)

## Installation

```bash
git clone <this-repo> spotify-mcp
cd spotify-mcp

# with uv (recommended)
uv sync

# or with pip
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Configuration

Set your Client ID (and optionally the redirect URI) as environment variables.
For local testing you can copy `.env.example` to `.env`, but MCP clients pass
these via their config `env` block (shown below).

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `SPOTIFY_CLIENT_ID` | ✅ | — | From your Spotify app dashboard. |
| `SPOTIFY_CLIENT_SECRET` | — | — | Optional. If set, uses the classic Authorization Code flow instead of PKCE. |
| `SPOTIFY_REDIRECT_URI` | — | `http://127.0.0.1:8888/callback` | Must exactly match a redirect URI on your app. |
| `SPOTIFY_MCP_CACHE` | — | `~/.spotify-mcp/token.json` | Where OAuth tokens are cached. |
| `RECCOBEATS_BASE_URL` | — | `https://api.reccobeats.com` | Base URL for the `get_audio_features` provider. |
| `LRCLIB_BASE_URL` | — | `https://lrclib.net` | Base URL for the `find_choruses` lyrics provider. |

### Authenticate first (recommended)

Sign in once from the terminal before wiring it into a client — this makes the
first-run browser flow easier to see:

```bash
spotify-mcp auth        # opens a browser to sign in
spotify-mcp status      # confirm you're authenticated
```

You can also trigger this later from within any MCP client by calling the
`authenticate` tool.

## Using with Claude Code

Register the server (adjust the command/path for your install):

```bash
claude mcp add spotify \
  --env SPOTIFY_CLIENT_ID=your_client_id_here \
  -- spotify-mcp
```

Or add it to your MCP config JSON directly:

```json
{
  "mcpServers": {
    "spotify": {
      "command": "spotify-mcp",
      "env": {
        "SPOTIFY_CLIENT_ID": "your_client_id_here"
      }
    }
  }
}
```

If `spotify-mcp` isn't on your `PATH`, use the full interpreter path instead,
e.g. `"command": "/path/to/spotify-mcp/.venv/bin/spotify-mcp"`, or
`"command": "uv"` with `"args": ["run", "spotify-mcp"]` and a `cwd`.

## Using with Coworker (or other MCP clients)

Any MCP client that launches a stdio server works. Point it at the `spotify-mcp`
command (or `python -m spotify_mcp`) with `SPOTIFY_CLIENT_ID` in the environment:

```json
{
  "mcpServers": {
    "spotify": {
      "command": "spotify-mcp",
      "env": { "SPOTIFY_CLIENT_ID": "your_client_id_here" }
    }
  }
}
```

Then just ask, e.g.:

- "Search Spotify for upbeat indie tracks from 2019."
- "Show me the songs in my *Focus* playlist."
- "Create a private playlist called *Roadtrip* and add these five songs."
- "How many choruses does *Don't Start Now* have, and how long is each one?"
- "For every song in my *Spin Tuesday* playlist, list the chorus timings."

## Scopes requested

The server requests the scopes needed for its tools:
`user-read-private`, `user-read-email`, `playlist-read-private`,
`playlist-read-collaborative`, `playlist-modify-private`,
`playlist-modify-public`.

## Security notes

- OAuth tokens are stored locally at `SPOTIFY_MCP_CACHE` with `0600` permissions
  and are **git-ignored**. Never commit them.
- PKCE means no client secret is stored on disk or passed around.
- The server only calls Spotify's official API endpoints over HTTPS.

## Development

```bash
# Run the server over stdio (what MCP clients invoke)
spotify-mcp

# Or as a module
python -m spotify_mcp

# Run the tests
uv run pytest
```

Project layout:

```
src/spotify_mcp/
  auth.py           # OAuth (PKCE) flow, token cache & refresh
  client.py         # Spotify Web API wrapper + response trimming
  audio_features.py # Tempo/key/energy via ReccoBeats (Spotify's is deprecated)
  structure.py      # Chorus/section detection from LRCLIB synced lyrics
  server.py         # FastMCP server and tool definitions
  __main__.py       # CLI: run server / auth / status
tests/              # Unit + stub-server tests (no network needed)
```

## License

MIT
