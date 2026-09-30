#!/bin/sh
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=${1:-}

if [ -z "$VERSION" ]; then
    echo "Usage: tools/release_github.sh MAJOR.MINOR.PATCH" >&2
    exit 2
fi
VERSION=${VERSION#v}
case "$VERSION" in
    *[!0-9.]*|*.*.*.*|.*|*.)
        echo "Error: version must have the form MAJOR.MINOR.PATCH" >&2
        exit 2
        ;;
esac
if [ "$(printf '%s' "$VERSION" | awk -F. '{print NF}')" -ne 3 ]; then
    echo "Error: version must have the form MAJOR.MINOR.PATCH" >&2
    exit 2
fi
TAG="v$VERSION"

if [ -n "$(git -C "$APP_ROOT" status --porcelain)" ]; then
    echo "Error: commit or stash application changes before releasing." >&2
    exit 1
fi
git -C "$APP_ROOT" fetch origin --tags
if git -C "$APP_ROOT" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "Error: tag already exists: $TAG" >&2
    exit 1
fi

BRANCH=$(git -C "$APP_ROOT" branch --show-current)
if [ -z "$BRANCH" ]; then
    echo "Error: cannot release from detached HEAD." >&2
    exit 1
fi
git -C "$APP_ROOT" diff --quiet "origin/$BRANCH...HEAD" || {
    echo "Error: local branch differs from origin/$BRANCH; push it first." >&2
    exit 1
}

git -C "$APP_ROOT" tag -a "$TAG" -m "Firmware $TAG"
git -C "$APP_ROOT" push origin "$TAG"
echo "Pushed $TAG; GitHub Actions will build and publish the release."
