#!/usr/bin/env bash
# Build sdist + wheel into dist/ for GitHub/Origin Releases or local pipx install.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-}"
if [[ -n "$VERSION" ]]; then
  # Optional: bump version in pyproject.toml + package __init__.
  # Usage: ./scripts/build-release.sh 0.2.0
  if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][a-zA-Z0-9]+)?$ ]]; then
    echo "error: version must look like 0.2.0 (got: $VERSION)" >&2
    exit 1
  fi
  python3 - "$VERSION" <<'PY'
import re
import sys
from pathlib import Path

ver = sys.argv[1]
root = Path(".")
pyproject = root / "pyproject.toml"
text = pyproject.read_text()
text2, n = re.subn(r'(?m)^version\s*=\s*"[^"]+"', f'version = "{ver}"', text, count=1)
if n != 1:
    raise SystemExit("could not update version in pyproject.toml")
pyproject.write_text(text2)

init = root / "src" / "melodify" / "__init__.py"
init_text = init.read_text()
init_text2, n = re.subn(
    r'(?m)^__version__\s*=\s*"[^"]+"',
    f'__version__ = "{ver}"',
    init_text,
    count=1,
)
if n != 1:
    raise SystemExit("could not update __version__ in src/melodify/__init__.py")
init.write_text(init_text2)
print(f"bumped version -> {ver}")
PY
fi

python3 -m pip install -q --upgrade build
rm -rf dist build src/*.egg-info melodify.egg-info
python3 -m build

echo
echo "Artifacts:"
ls -la dist/
echo
echo "Install locally with pipx:"
WHEEL="$(ls dist/melodify-*-py3-none-any.whl | head -1)"
echo "  pipx install '${WHEEL}'"
echo "  # or: pipx install --force '${WHEEL}'"
