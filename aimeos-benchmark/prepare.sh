#!/bin/sh
# Rebuild apps/aimeos: clone Aimeos at the pinned commit and add the benchmark files.
# The benchmark files in this folder are under the project's MIT LICENSE.
# Aimeos itself is MIT-licensed by its authors (see apps/aimeos/LICENSE after cloning).
set -eu
COMMIT=919720089ad5ff6e63eee9c0aaf4a42c2cc27340
HERE=$(cd "$(dirname "$0")" && pwd)
TARGET="$HERE/../apps/aimeos"
if [ -e "$TARGET" ]; then
  ACTUAL=$(git -C "$TARGET" rev-parse HEAD 2>/dev/null || echo none)
  if [ "$ACTUAL" != "$COMMIT" ]; then
    echo "error: $TARGET exists at commit $ACTUAL, expected $COMMIT. Move it away first." >&2
    exit 1
  fi
else
  git clone --quiet https://github.com/aimeos/aimeos.git "$TARGET"
  git -C "$TARGET" checkout --quiet "$COMMIT"
fi
for FILE in Dockerfile.benchmark compose.benchmark.yaml entrypoint.benchmark.sh run.py traffic.mjs; do
  cp "$HERE/$FILE" "$TARGET/$FILE"
done
echo "apps/aimeos is at $COMMIT with the benchmark files."
