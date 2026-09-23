#!/bin/sh
set -eu

# Zero's 0.3.4 release predates std.term. Pin a verified upstream snapshot
# that provides the native terminal and process APIs used by this application.
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
revision=7e1a64d27cc37671df31c6370890bce86f5135e1
checksum=a2536e06cba978c8e146bd294b08fd2262f44fe9d19b45918312ef7a96a6885b
source_dir="$project_dir/.tools/zerolang-$revision"
archive="$project_dir/.tools/zerolang-$revision.tar.gz"

for program in curl tar make cc node; do
    command -v "$program" >/dev/null 2>&1 || { printf 'Missing build dependency: %s\n' "$program" >&2; exit 1; }
done
node -e 'if (Number(process.versions.node.split(".")[0]) < 24) { console.error("Building Zero requires Node 24 or newer."); process.exit(1); }'
mkdir -p "$project_dir/.tools/bin"
if [ ! -f "$archive" ]; then
    curl --fail --location --show-error --retry 3 \
        "https://codeload.github.com/vercel-labs/zerolang/tar.gz/$revision" -o "$archive.part"
    mv "$archive.part" "$archive"
fi
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$archive" | cut -d ' ' -f 1)
else
    actual=$(shasum -a 256 "$archive" | cut -d ' ' -f 1)
fi
[ "$actual" = "$checksum" ] || { printf 'Zero source checksum mismatch.\n' >&2; exit 1; }
if [ ! -d "$source_dir" ]; then
    tar -xzf "$archive" -C "$project_dir/.tools"
fi
# The pinned backend supports these frame offsets, but caps individual fixed
# buffers at 128 KiB. File tools need bounded JSON buffers for two 1000 KB
# fragments (up to 14 MiB with escaping). Keep the patch exact and repeatable;
# the application reserves a 64 MiB stack before entering its buffer frames.
node "$project_dir/scripts/configure-zero-buffers.mjs" "$source_dir/native/zero-c/include/zero.h"
node "$project_dir/scripts/configure-zero-live.mjs" "$source_dir/native/zero-c/src/c_import.c"
make -C "$source_dir/native/zero-c"
cp "$source_dir/.zero/bin/zero" "$project_dir/.tools/bin/zero"
printf '%s\n' "$revision" > "$project_dir/.tools/compiler-revision"
printf '%s\n' '16777216' > "$project_dir/.tools/compiler-frame-limit"
printf '%s\n' '2' > "$project_dir/.tools/compiler-live-abi"
printf '\nCompiler ready. Run ./scripts/build.sh, then ./dist/zero-code.\n'
