#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
DIST_DIR="$REPO_ROOT/dist"
PROJECT_NAME=$(basename "$REPO_ROOT")

if [ "${1-}" != "" ]; then
  VERSION="$1"
else
  DATE_TAG=$(date -u +%Y%m%d)
  if command -v git >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
    COMMIT_TAG=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
    VERSION="$DATE_TAG-$COMMIT_TAG"
  else
    TIME_TAG=$(date -u +%H%M%S)
    VERSION="$DATE_TAG-$TIME_TAG"
  fi
fi

ARCHIVE_BASENAME="$PROJECT_NAME-$VERSION"
TAR_PATH="$DIST_DIR/$ARCHIVE_BASENAME.tar.gz"
ZIP_PATH="$DIST_DIR/$ARCHIVE_BASENAME.zip"
TMP_PARENT=${TMPDIR:-/tmp}
TMP_STAGE=$(mktemp -d "$TMP_PARENT/$PROJECT_NAME-package.XXXXXX")
STAGE_DIR="$TMP_STAGE/$ARCHIVE_BASENAME"

cleanup() {
  rm -rf "$TMP_STAGE"
}
trap cleanup EXIT INT TERM

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Error: required command not found: %s\n' "$1" >&2
    exit 1
  fi
}

need_cmd tar
need_cmd zip
need_cmd sha256sum
need_cmd wc
need_cmd awk

mkdir -p "$DIST_DIR"
mkdir -p "$STAGE_DIR"

tar -C "$REPO_ROOT" \
  --exclude='./.git' \
  --exclude='./dist' \
  --exclude='./__pycache__' \
  --exclude='./.pytest_cache' \
  --exclude='./.mypy_cache' \
  --exclude='./.ruff_cache' \
  --exclude='./.tox' \
  --exclude='./.nox' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  --exclude='*.pyd' \
  -cf - . | tar -C "$STAGE_DIR" -xf -

tar -C "$TMP_STAGE" -czf "$TAR_PATH" "$ARCHIVE_BASENAME"
(cd "$TMP_STAGE" && zip -qr "$ZIP_PATH" "$ARCHIVE_BASENAME")

(cd "$DIST_DIR" && sha256sum "$(basename "$TAR_PATH")" > "$(basename "$TAR_PATH").sha256")
(cd "$DIST_DIR" && sha256sum "$(basename "$ZIP_PATH")" > "$(basename "$ZIP_PATH").sha256")

tar_size=$(wc -c < "$TAR_PATH" | awk '{print $1}')
zip_size=$(wc -c < "$ZIP_PATH" | awk '{print $1}')
tar_sha=$(awk '{print $1}' "$TAR_PATH.sha256")
zip_sha=$(awk '{print $1}' "$ZIP_PATH.sha256")

printf 'Release package complete\n'
printf 'Version: %s\n' "$VERSION"
printf 'Output directory: %s\n' "$DIST_DIR"
printf ' - %s (%s bytes) sha256=%s\n' "$(basename "$TAR_PATH")" "$tar_size" "$tar_sha"
printf ' - %s (%s bytes) sha256=%s\n' "$(basename "$ZIP_PATH")" "$zip_size" "$zip_sha"
printf ' - %s\n' "$(basename "$TAR_PATH").sha256"
printf ' - %s\n' "$(basename "$ZIP_PATH").sha256"
