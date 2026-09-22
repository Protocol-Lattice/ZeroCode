#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compiler=${ZERO_COMPILER:-"$project_dir/.tools/bin/zero"}
if [ ! -x "$compiler" ]; then
    printf 'Set up the pinned Zero compiler first: ./scripts/setup-zero.sh\n' >&2
    exit 1
fi
if [ -z "${ZERO_COMPILER:-}" ] && { [ ! -f "$project_dir/.tools/compiler-frame-limit" ] || [ "$(cat "$project_dir/.tools/compiler-frame-limit")" != 16777216 ]; }; then
    printf 'Update the pinned compiler for 1000 KB file tools: ./scripts/setup-zero.sh\n' >&2
    exit 1
fi
cd "$project_dir"
mkdir -p dist
"$compiler" build --target host --out "$project_dir/dist/zero-code"
# Graph patches do not rewrite their readable projections. Keep the source
# package coherent before an installer snapshots it for executable evolution.
"$compiler" export "$project_dir"
