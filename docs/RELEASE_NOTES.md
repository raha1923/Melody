# Release notes template

Copy this into the GitHub/Origin release body (or keep it updated before tagging).
The release workflow attaches `dist/*.whl` and `dist/*.tar.gz` when a `v*` tag is pushed on GitHub.

## v0.3.0

### Features

- **`melodify login`** — phone + SMS OTP (same as desktop.melodify.app). Persists `session.json` (0600) under the OS data dir. Also `logout` / `whoami`.
- TUI shows a login screen when unauthenticated. `.env` tokens remain optional overrides.
- AuthClient headers aligned with desktop PWA (`Mac OS-Desktop`, omit empty auth on `/getSms`).

### Install

```bash
pipx install "https://github.com/raha1923/Melody/releases/download/v0.3.0/melodify-0.3.0-py3-none-any.whl"
melodify login
```

## v0.2.1

### Fixes

- **Queue panel blink** — Up next no longer rebuilds on every ~200ms IPC tick. Progress/time still update; the compact Up next strip redraws only when queue index/contents change. Press `u` to expand/collapse.

## v0.2.0

### Fixes

- **Auto-advance lag** — next track starts within ~1s of EOF instead of waiting 20–30s for the mpv process to exit (HTTP keep-alive). Uses mpv `eof-reached` IPC + `--keep-open=no`.
- **Manual play restart** — choosing a song no longer replays from the start after ~30s. Play starts before `/listens`; stale exclusive workers are aborted via a play-generation token; select is debounced; MPRIS/Now Playing “Play” echoes are ignored for 2.5s after start.

### Install

**Ubuntu / macOS (pipx from a release wheel):**

Prefer the **v0.3.0** install one-liner above (repo: [raha1923/Melody](https://github.com/raha1923/Melody)).

Still need `mpv`. On Ubuntu for MPRIS: apt `python3-gi` + `gir1.2-glib-2.0`, then `pipx inject` as in the README. On macOS for Now Playing: `pipx inject melodify 'pyobjc-framework-MediaPlayer>=10'`.

### Packaging

- `scripts/build-release.sh` — local sdist/wheel build (optional version bump arg)
- `.github/workflows/release.yml` — tag `v0.3.0` → GitHub Release + artifacts

---

## Next release checklist

1. Bump version: `./scripts/build-release.sh X.Y.Z` (or edit `pyproject.toml` + `__init__.py`)
2. Update this file’s top section with user-facing changes
3. Commit, push, tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
4. Confirm CI attached the wheel/sdist (GitHub) or upload `dist/*` manually on Origin
