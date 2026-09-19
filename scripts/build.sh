#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compiler=${ZERO_COMPILER:-"$project_dir/.tools/bin/zero"}
if [ ! -x "$compiler" ]; then
    printf 'Set up the pinned Zero compiler first: ./scripts/setup-zero.sh\n' >&2
    exit 1
fi
cd "$project_dir"
mkdir -p dist
"$compiler" build --target host --out "$project_dir/dist/zero-code"
