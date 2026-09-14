#!/bin/sh
# Materialize examples/fixtures/greeter as a git repository at a fixed path,
# so `cargo generate-lockfile` can resolve hello-rust-git's git dependency.
# Identity and dates are pinned, so the resulting commit hash is the same on
# every machine and matches the one recorded in Cargo.lock.
#
# Only needed to (re)generate Cargo.lock.  The Guix build never fetches this
# repository: manifest.scm maps its URL straight to the fixture directory.
#
# Usage: setup-fixture.sh [DEST]      (default DEST: /tmp/holocronix-fixtures/greeter)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
src="$here/../fixtures/greeter"
dest=${1:-/tmp/holocronix-fixtures/greeter}

if [ -e "$dest/.git" ]; then
    echo "fixture already present at $dest" >&2
    git -C "$dest" rev-parse HEAD
    exit 0
fi

mkdir -p "$dest"
cp -R "$src/." "$dest/"
cd "$dest"
git init -q -b main
git add -A
GIT_AUTHOR_NAME=holocronix GIT_AUTHOR_EMAIL=fixture@holocronix.invalid \
GIT_COMMITTER_NAME=holocronix GIT_COMMITTER_EMAIL=fixture@holocronix.invalid \
GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z \
    git -c commit.gpgsign=false -c core.hooksPath=/dev/null \
        commit -q -m 'greeter fixture'
git rev-parse HEAD
