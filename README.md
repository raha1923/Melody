# Melodify (terminal)

CLI/TUI music client for [Melodify](https://desktop.melodify.app) on **Ubuntu** and **macOS**. Search artists, browse albums, pick songs, and **stream** through `mpv` (or `ffplay`) so tracks are never buffered entirely into Python memory.

This is not a web app — no local HTTP server is started.

## Requirements

- Python 3.10+
- `mpv` (preferred) or `ffmpeg`/`ffplay`
- A Melodify account (phone + SMS login)

### Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv mpv \
  python3-gi gir1.2-glib-2.0   # system PyGObject for MPRIS (do not pip-build it)
```

### macOS

```bash
brew install mpv python
# Apple Silicon / Homebrew Python is fine; system python3 also works on recent macOS
```

## Install (recommended: pipx + release wheel)

No clone or manual venv required. Grab a wheel from the [GitHub Releases](https://github.com/OWNER/REPO/releases) page (or build one with `./scripts/build-release.sh`), then:

### Ubuntu

```bash
sudo apt update
sudo apt install -y mpv pipx python3-gi gir1.2-glib-2.0
pipx ensurepath

# One-liner once a release exists (replace OWNER/REPO and version):
pipx install "https://github.com/OWNER/REPO/releases/download/v0.2.0/melodify-0.2.0-py3-none-any.whl"

# Optional MPRIS (GNOME/KDE media keys) — inject into the pipx env:
pipx inject melodify StrEnum 'emoji>=2.8,<3' 'pydbus>=0.6,<0.7' 'unidecode>=1.3,<2'
pipx inject --pip-args='--no-deps' melodify mpris-server

melodify doctor
```

Or download the `.whl` from the release assets and run `pipx install ./melodify-0.2.0-py3-none-any.whl`.

### macOS

```bash
brew install mpv pipx
pipx ensurepath

pipx install "https://github.com/OWNER/REPO/releases/download/v0.2.0/melodify-0.2.0-py3-none-any.whl"

# Optional Now Playing / media keys:
pipx inject melodify 'pyobjc-framework-MediaPlayer>=10'

melodify doctor
```

`OWNER/REPO` is a placeholder until the public repository URL is set. Until then, build a wheel locally:

```bash
./scripts/build-release.sh          # writes dist/*.whl
pipx install dist/melodify-*-py3-none-any.whl
```

PyPI publish is a later option (`pipx install melodify`); packaging already exposes the `melodify` console entry point.

### Dev install from a clone

#### Ubuntu

```bash
cd /path/to/this/repo
git pull

# Critical: system-site-packages so apt's python3-gi (`gi`) is visible in the venv.
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

pip install -e ".[mpris]"            # core + pure-Python MPRIS deps
pip install --no-deps mpris-server   # uses apt PyGObject — do not omit --no-deps

melodify doctor
```

#### macOS

```bash
cd /path/to/this/repo
git pull

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[macos]"   # core + PyObjC MediaPlayer (Now Playing / media keys)

melodify doctor
```

Data files live under:

| OS | Path |
|---|---|
| Linux | `~/.local/share/melodify/` |
| macOS | `~/Library/Application Support/melodify/` |

On macOS, if you previously used the Linux-style `~/.local/share/melodify/` path, files are copied into Application Support on first use.

## Login (recommended)

No need to copy tokens between machines. Melodify uses the same **phone + SMS** flow as [desktop.melodify.app](https://desktop.melodify.app):

```bash
melodify login
# Phone (09xxxxxxxxx): …
# SMS code: …

melodify whoami
melodify          # or: melodify tui
```

Session is stored at:

| OS | File |
|---|---|
| Linux | `~/.local/share/melodify/session.json` (mode `0600`) |
| macOS | `~/Library/Application Support/melodify/session.json` |

A per-install `device.json` (device-id) is created automatically — new logins do not reuse a shared sample device forever.

```bash
melodify logout          # deletes session.json (keeps device.json)
```

If the TUI is started with no session, it shows an in-app login form (same OTP flow).

### Legacy `.env` (optional)

Environment variables still work and **override** the session file when set (compat for older setups):

```bash
cp .env.example .env   # optional
```

| Variable | Description |
|---|---|
| `MELODIFY_AUTHORIZATION` | Session / download token |
| `MELODIFY_USER_ID` | Numeric user id |
| `MELODIFY_DEVICE_ID` | Device id header |
| `MELODIFY_DEVICE_TOKEN` | Device token UUID |

Optional overrides: `MELODIFY_API_BASE`, `MELODIFY_APP_VERSION`, `MELODIFY_PWA_VERSION`, `MELODIFY_PLATFORM`, `MELODIFY_DEVICE_NAME`, `MELODIFY_STREAM_QUALITY`, `MELODIFY_PLAYER`, `MELODIFY_MPRIS`, `MELODIFY_NOWPLAYING`.

**Never commit `.env` or `session.json`.**

## Run

```bash
melodify login            # once per machine
melodify                  # interactive TUI
melodify whoami
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
| `…/melodify/recent.json` | Recently played list (deduped by track id, capped at 50) |
| `…/melodify/playback.json` | Scrubber positions for continue/seek |

(`…` = `~/.local/share` on Linux, `~/Library/Application Support` on macOS.)

- Every successful play start (and pause/stop/finish) **upserts** the track to the front of `recent.json`.
- `melodify last` / TUI **Last played** prefer that local list. If it is empty, they fall back to online `getUserMostListenedTracks` (after a short `recently_listened` probe).
- `melodify continue` / TUI **Continue** resume the first track from that preferred list. Seek uses `playback.json` (or an API progress field when present); otherwise starts at 0.

### TUI layout (nvlc-ish)

```
┌ Browse (search / lists)     │ Now playing + progress bar     ┐
│ Songs · Artists · Albums    │ Up next (▶ current + a few)    │
│ Last played                 │ Space pause · n/b · u expand   │
└─────────────────────────────┴────────────────────────────────┘
```

Progress/time refresh from mpv IPC ~5×/sec. The **Up next** panel is compact by default and only redraws when the queue index or contents change (not on every tick — that used to blink). Press `u` to expand/collapse upcoming rows.

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
| `u` | Toggle Up next compact ↔ expanded |
| `←` / `→` | Seek −5s / +5s |
| Shift+`←` / `→` | Seek −10s / +10s |
| `-` / `=` | Volume down / up |
| `s` | Stop playback |
| Backspace | Go back |
| `q` | Quit |

### OS media controls

#### Linux (MPRIS)

Publishes **MPRIS2** as `org.mpris.MediaPlayer2.Melodify` (GNOME/KDE media popup, media keys, `playerctl`).

```bash
playerctl -p Melodify status
playerctl -p Melodify play-pause
```

Needs: apt `python3-gi` + `gir1.2-glib-2.0`, session D-Bus, `pip install -e ".[mpris]"`, then `pip install --no-deps mpris-server`. Set `MELODIFY_MPRIS=0` to disable.

#### macOS (Now Playing)

Uses Apple’s **MediaPlayer** framework (via PyObjC) so Melodify appears in **Control Center → Now Playing** and responds to keyboard media keys (play/pause, next, previous; scrub when the OS sends a seek).

```bash
pip install -e ".[macos]"   # pulls pyobjc-framework-MediaPlayer
```

Limitations: artwork in Control Center is best-effort (title/artist/duration always set). Headless SSH sessions without a GUI won’t show Now Playing. Set `MELODIFY_NOWPLAYING=0` to disable.

```bash
melodify doctor   # platform-aware (MPRIS on Linux, Now Playing on macOS)
```

### Long lists / pagination

Melodify’s API returns about **40 items per page**. The client automatically fetches further pages (artist songs/albums, album tracks, most-listened) up to 500 items so the TUI list contains the full catalog, not just the first page. Use ↑↓ / PgUp/PgDn (or `j`/`k`) once the list is focused to reach items below the fold.

### Pagination / Load more

Melodify APIs return ~40 items per page. On open, Melodify **auto-prefetches** further pages in the background until the API says `end` or **500** items (status shows `Loaded 80…`).

- Beyond 500 (or if prefetch stopped early), use **`m`**, **Load more**, or the bottom **── Load more ──** row
- New rows **append** (already-loaded tracks stay)
- Playing from a list uses the **full loaded set** as the queue; near the end of the queue Melodify also pulls the next page when more exist

Auto-prefetch applies to: search Songs/Artists/Albums, artist Songs/Albums, album tracks, and online last-played (local recent is already complete).

### Play queue

When you play a song from **search results**, **artist tracks**, **album tracks**, or **last played**, that list becomes the in-memory queue and playback starts at the selected item. Continue listening loads the preferred history list (local recent, else online) as the queue.

- **No wrap** at the edges — next at the end / prev at the start is a no-op (with a short notice).
- When the current track ends, Melodify **auto-advances** to the next queue item if one exists.
- If more Melodify pages exist for that list, they are pulled into the queue automatically when you get close to the end.
- The right pane shows a quiet **Up next** strip (current + a few upcoming), not a full flashing queue list. `u` expands it.

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

## Releases / packaging

| Piece | Role |
|---|---|
| `pyproject.toml` | Package metadata + `melodify` entry point (`melodify = melodify.cli:main`) |
| `scripts/build-release.sh` | Build sdist + wheel into `dist/` (optional version bump: `./scripts/build-release.sh 0.2.1`) |
| `.github/workflows/release.yml` | On tag `v*`, build artifacts and attach them to a GitHub Release |
| `docs/RELEASE_NOTES.md` | User-facing notes template + checklist |

**Tag a release (GitHub):** bump version → commit → `git tag v0.2.0 && git push origin v0.2.0`. CI attaches the wheel/sdist.

**Origin:** Origin-native release file attach can be limited. Preferred path is GitHub Releases (above) or run `./scripts/build-release.sh` and upload `dist/*.whl` / `dist/*.tar.gz` to whatever release UI your forge provides. See `docs/RELEASE_NOTES.md`.

## Troubleshooting

- **Not logged in** — run `melodify login` (or use the TUI login form). Legacy: `.env` tokens.
- **Auth rejected (401/403/406)** — `melodify logout` then `melodify login` again.
- **502 / timeouts** — Melodify’s API is occasionally flaky; retry.
- **No sound** — confirm `mpv` works (`mpv --version`) and your session has stream rights for the track.
- **Next track waits ~30s** — fixed in 0.2.0 (EOF via mpv `eof-reached`, not process exit). Upgrade the wheel or pull latest.
- **Song restarts after ~30s on manual play** — fixed in 0.2.0 (play before `/listens` + generation guards). Upgrade or pull latest.
