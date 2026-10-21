#!/usr/bin/env bash
# Push branches + tag v0.3.0 to github.com/raha1923/Melody and create a Release.
# Requires: gh auth login (repo scope) OR GITHUB_TOKEN in the environment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
TAG="v${VERSION}"
REMOTE="${GITHUB_REMOTE:-github}"
REPO_URL="https://github.com/raha1923/Melody.git"

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  git remote add "$REMOTE" "$REPO_URL"
fi

echo "→ Ensuring $REMOTE points at $REPO_URL"
git remote set-url "$REMOTE" "$REPO_URL"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "→ Pushing branch $BRANCH and main"
git push -u "$REMOTE" "$BRANCH"
git push "$REMOTE" main

echo "→ Building wheel/sdist"
./scripts/build-release.sh

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Tag $TAG already exists locally"
else
  git tag -a "$TAG" -m "Melodify $TAG"
fi

echo "→ Pushing tag $TAG (triggers .github/workflows/release.yml if Actions enabled)"
git push "$REMOTE" "$TAG"

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if gh release view "$TAG" --repo raha1923/Melody >/dev/null 2>&1; then
    echo "Release $TAG already exists — uploading assets"
    gh release upload "$TAG" dist/* --repo raha1923/Melody --clobber
  else
    echo "→ Creating GitHub Release $TAG with artifacts"
    gh release create "$TAG" dist/* \
      --repo raha1923/Melody \
      --title "$TAG" \
      --notes-file docs/RELEASE_NOTES.md
  fi
  echo "Release: https://github.com/raha1923/Melody/releases/tag/$TAG"
else
  echo "gh not authenticated — tag push alone is enough if Actions is enabled on the repo."
  echo "Or run: gh auth login && gh release create $TAG dist/* --repo raha1923/Melody --title $TAG"
fi

echo
echo "Install:"
echo "  pipx install \"https://github.com/raha1923/Melody/releases/download/${TAG}/melodify-${VERSION}-py3-none-any.whl\""
