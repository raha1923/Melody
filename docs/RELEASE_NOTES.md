# Release notes template

Copy this into the GitHub/Origin release body (or keep it updated before tagging).
The release workflow attaches `dist/*.whl` and `dist/*.tar.gz` when a `v*` tag is pushed on GitHub.

## v0.2.1

### Fixes

- **Queue panel blink** — Up next no longer rebuilds on every ~200ms IPC tick. Progress/time still update; the compact Up next strip redraws only when queue index/contents change. Press `u` to expand/collapse.

## v0.2.0

### Fixes

- **Auto-advance lag** — next track starts within ~1s of EOF instead of waiting 20–30s for the mpv process to exit (HTTP keep-alive). Uses mpv `eof-reached` IPC + `--keep-open=no`.
- **Manual play restart** — choosing a song no longer replays from the start after ~30s. Play starts before `/listens`; stale exclusive workers are aborted via a play-generation token; select is debounced; MPRIS/Now Playing “Play” echoes are ignored for 2.5s after start.

### Install

**Ubuntu / macOS (pipx from a release wheel):**

```bash
# Replace OWNER/REPO and VERSION with the release you downloaded
pipx install "https://github.com/OWNER/REPO/releases/download/v0.2.0/melodify-0.2.0-py3-none-any.whl"
```

Or download the `.whl` from the release page, then:

```bash
pipx install ./melodify-0.2.0-py3-none-any.whl
```

Still need `mpv` (and Melodify session env — see README). On Ubuntu for MPRIS: apt `python3-gi` + `gir1.2-glib-2.0`, then `pipx inject melodify StrEnum emoji pydbus unidecode` and `pipx inject --pip-args='--no-deps' melodify mpris-server`. On macOS for Now Playing: `pipx inject melodify 'pyobjc-framework-MediaPlayer>=10'`.

### Packaging

- `scripts/build-release.sh` — local sdist/wheel build (optional version bump arg)
- `.github/workflows/release.yml` — tag `v0.2.0` → GitHub Release + artifacts

---

## Next release checklist

1. Bump version: `./scripts/build-release.sh X.Y.Z` (or edit `pyproject.toml` + `__init__.py`)
2. Update this file’s top section with user-facing changes
3. Commit, push, tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
4. Confirm CI attached the wheel/sdist (GitHub) or upload `dist/*` manually on Origin
