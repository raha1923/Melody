# Melodify (terminal)

Ubuntu CLI/TUI music client for [Melodify](https://desktop.melodify.app). Search artists, browse albums, pick songs, and **stream** playback through `mpv` (or `ffplay`) so tracks are never buffered entirely into Python memory.

This is not a web app — no local HTTP server is started.

## Requirements (Ubuntu)

- Python 3.10+
- `mpv` (preferred) or `ffmpeg`/`ffplay`
- A Melodify session token + device headers (see below)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv mpv \
  python3-gi gir1.2-glib-2.0   # system PyGObject for MPRIS (do not pip-build it)
```

## Install / update

```bash
cd /path/to/this/repo
git pull

# Critical: system-site-packages so apt's python3-gi (`gi`) is visible in the venv.
# Without this, pip tries to compile PyGObject/pycairo and fails (missing cairo/pkg-config).
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

pip install -e ".[mpris]"        # core + pure-Python MPRIS deps (StrEnum, pydbus, …)
pip install --no-deps mpris-server   # uses apt PyGObject — do not omit --no-deps

melodify doctor                  # verifies MPRIS wiring
```

If you already created a venv **without** `--system-site-packages`, recreate it:

```bash
deactivate
rm -rf .venv
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[mpris]"
pip install --no-deps mpris-server
```

## Configure

Copy the example env file and fill in values from your Melodify desktop/PWA session:

```bash
cp .env.example .env
```

Required variables:

| Variable | Description |
|---|---|
| `MELODIFY_AUTHORIZATION` | Session / download token (`authorization` header) |
| `MELODIFY_USER_ID` | Numeric user id (`user-id` / `melodify_token`) |
| `MELODIFY_DEVICE_ID` | Device id header |
| `MELODIFY_DEVICE_TOKEN` | Device token UUID |

Optional:

| Variable | Default |
|---|---|
| `MELODIFY_API_BASE` | `https://melodify.pw/api/desktop/v9` |
| `MELODIFY_APP_VERSION` | `9.1.3` |
| `MELODIFY_PWA_VERSION` | `9.2.6 Desktop` |
| `MELODIFY_PLATFORM` | `Linux-Desktop` |
| `MELODIFY_STREAM_QUALITY` | `320` |
| `MELODIFY_PLAYER` | `mpv` |
| `MELODIFY_MPRIS` | `1` (set `0` to disable OS media controls) |

**Never commit `.env`.** Tokens grant access to your Melodify account.

## Run

Interactive TUI:

```bash
melodify
# or
melodify tui
```

CLI helpers:

```bash
melodify search "googoosh"
melodify artist 251
melodify album 926
melodify play "برای من"
melodify last                 # local recently-played (online fallback)
melodify continue             # resume top recent track
```

### Continue listening / last played

Melodify’s PWA “recently listened” list is **offline** (local Redux/IndexedDB). Melodify mirrors that locally:

| File | Purpose |
|---|---|
| `~/.local/share/melodify/recent.json` | Recently played list (deduped by track id, capped at 50) |
| `~/.local/share/melodify/playback.json` | Scrubber positions for continue/seek |

- Every successful play start (and pause/stop/finish) **upserts** the track to the front of `recent.json`.
- `melodify last` / TUI **Last played** prefer that local list. If it is empty, they fall back to online `getUserMostListenedTracks` (after a short `recently_listened` probe).
- `melodify continue` / TUI **Continue** resume the first track from that preferred list. Seek uses `playback.json` (or an API progress field when present); otherwise starts at 0.

### TUI layout (nvlc-ish)

```
┌ Browse (search / lists)     │ Now playing + progress bar     ┐
│ Songs · Artists · Albums    │ Queue (▶ current, upcoming)    │
│ Last played                 │ Space pause · n/b next/prev    │
└─────────────────────────────┴────────────────────────────────┘
```

Right pane updates from mpv IPC ~2×/sec while a track is playing.

### TUI keys

| Key | Action |
|---|---|
| `/` | Focus search |
| Enter | Search / open / play highlighted song |
| ↑ / ↓ / `j` `k` | Move highlight in the focused list |
| PgUp / PgDn | Jump by page in long lists |
| Home / End | Jump to first / last list item |
| `m` / **Load more** | Fetch the next Melodify page into the focused list |
| `c` | Continue listening (local recent first) |
| `l` | Load last-played list (local recent first) |
| `p` | Play highlighted song |
| Space | Pause / resume (mpv IPC) |
| `n` / `>` | Next in queue |
| `b` / `<` | Previous in queue |
| `←` / `→` | Seek −5s / +5s |
| Shift+`←` / `→` | Seek −10s / +10s |
| `-` / `=` | Volume down / up |
| `s` | Stop playback |
| Backspace | Go back |
| `q` | Quit |

### OS media controls (MPRIS)

Melodify already speaks the Linux media APIs: it publishes **MPRIS2** on the session D-Bus as `org.mpris.MediaPlayer2.Melodify` and installs `~/.local/share/applications/melodify.desktop`.

That hooks into:

- GNOME / KDE media popups (title, artist, artwork)
- Keyboard media keys (play/pause, next, previous)
- `playerctl` and other MPRIS clients (seek + volume when using `mpv`)

```bash
# while Melodify is running on your desktop:
playerctl -p Melodify status
playerctl -p Melodify play-pause
playerctl -p Melodify next

# offline check (no Melodify account needed):
melodify doctor
```

Needs: apt `python3-gi` + `gir1.2-glib-2.0`, a desktop session bus, `pip install -e ".[mpris]"`, then `pip install --no-deps mpris-server` (never `pip install PyGObject` — that compiles cairo). Set `MELODIFY_MPRIS=0` to disable.

### Long lists / pagination

Melodify’s API returns about **40 items per page**. The client automatically fetches further pages (artist songs/albums, album tracks, most-listened) up to 500 items so the TUI list contains the full catalog, not just the first page. Use ↑↓ / PgUp/PgDn (or `j`/`k`) once the list is focused to reach items below the fold.

### Pagination / Load more

Melodify APIs return pages (~40 tracks). The TUI loads the first page, then:

- Scroll onto the bottom **── Load more (m) ──** row, press **`m`**, **PgDn/End** at the bottom, or click **Load more**
- New rows **append** (already-loaded tracks stay)
- Playing from a list uses the **full loaded set** as the queue; near the end of the queue Melodify **prefetches** the next page when the API still has more (`end: false`)

Applies to: search Songs/Artists/Albums, artist Songs/Albums, album tracks, and online last-played (local recent is already complete).

### Play queue

When you play a song from **search results**, **artist tracks**, **album tracks**, or **last played**, that list becomes the in-memory queue and playback starts at the selected item. Continue listening loads the preferred history list (local recent, else online) as the queue.

- **No wrap** at the edges — next at the end / prev at the start is a no-op (with a short notice).
- When the current track ends, Melodify **auto-advances** to the next queue item if one exists.
- If more Melodify pages exist for that list, they are pulled into the queue automatically when you get close to the end.

### CLI playback controls

While `melodify play` / `melodify continue` is running in a TTY with **mpv**:

| Key | Action |
|---|---|
| Space / `p` | Pause / resume |
| `n` / `>` | Next in queue |
| `b` / `<` | Previous in queue |
| `←` / `→` | Seek ±5s |
| `[` / `]` | Seek ±10s |
| `-` / `=` | Volume ±5 |
| `s` / `q` | Stop |

Pause/seek/volume/next need `mpv` (JSON IPC). `ffplay` can only stop.

## How playback works

1. Track metadata from the API includes CDN handle URLs under `mp3s[].name`.
2. The client appends `type=pwa`, `melodify_token`, `download_token`, and a `stamp…` query.
3. `cdn.melodify.pw` responds with a short-lived **307** to `s*-cdn.melodify.pw`.
4. `mpv`/`ffplay` follows the redirect and streams audio — Melodify’s Python process only holds the URL, not the file.
5. With `mpv`, Melodify opens a Unix JSON IPC socket (`--input-ipc-server`) for pause, seek, and volume without re-buffering the track.

## API surface used

Base: `https://melodify.pw/api/desktop/v9`

- `POST /search` — `{q, type, offset}` (`type=6` all, `1` tracks, `2` artists, `3` albums)
- `GET /getArtistData?artist_id=`
- `POST /getArtistTracks` — `{artist_id, offset, sort_by}`
- `POST /getArtistAlbums` — `{artist_id, offset}`
- `POST /getAlbumTracks` — `{album_id, offset}`
- `POST /listens` — record a listen `{track_id, is_demo}`
- `POST /getUserMostListenedTracks` — online listening history
- `POST /getPlaylistTracksByType` — e.g. `type=recently_listened` / `most_listened`
- Play: `GET https://cdn.melodify.pw/handle/v2/tracks/{hash}/{quality}/{device-token}?…`

See internal notes (agent store) for reverse-engineering details.

## Troubleshooting

- **Missing env vars** — copy `.env.example` → `.env` and fill all four required keys.
- **Auth rejected (401/403)** — refresh the token from a logged-in desktop.melodify.app session.
- **502 / timeouts** — Melodify’s API is occasionally flaky; retry.
- **No sound** — confirm `mpv` works (`mpv --version`) and your session has stream rights for the track.
