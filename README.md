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
```

Project layout:

```
src/spotify_mcp/
  auth.py     # OAuth (PKCE) flow, token cache & refresh
  client.py   # Spotify Web API wrapper + response trimming
  server.py   # FastMCP server and tool definitions
  __main__.py # CLI: run server / auth / status
```

## License

MIT
