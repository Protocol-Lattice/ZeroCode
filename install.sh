#!/bin/sh
# Install from a checkout or via: curl -fsSL <raw GitHub URL>/install.sh | sh
set -eu

fail() {
    printf 'zero code install: %s\n' "$*" >&2
    exit 1
}

require() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing dependency: $1. See the README installation prerequisites."
}

shell_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

usage() {
    cat <<'USAGE'
Install zero-code on Linux or macOS.

Usage: sh install.sh [options]
  --prefix DIR       Install into DIR/bin and DIR/libexec (default: ~/.local)
  --binary FILE      Install an already-built native binary instead of building
  --ref REF          Download and build a GitHub tag, branch, or commit
  --no-modify-path   Do not update shell startup files
  --uninstall        Remove zero code from the selected prefix
  --help             Show this help

By default a checkout is built locally; a standalone or piped installer downloads
the main branch. Builds need Node.js 24+, a C compiler, libcurl headers, make,
curl, tar, and Git.
The installed application does not need Node.js or the Zero compiler.
USAGE
}

add_to_profile() {
    profile=$1
    # Match the exact export, so repeated installs do not add duplicate entries.
    if [ ! -f "$profile" ] || ! grep -Fqx -- "$path_line" "$profile"; then
        mkdir -p "$(dirname "$profile")"
        printf '\n# zero-code\n%s\n' "$path_line" >> "$profile"
        printf 'Added PATH entry to %s\n' "$profile"
    fi
}

configure_path() {
    quoted_bin=$(shell_quote "$bin_dir")
    path_line="export PATH=$quoted_bin:\"\$PATH\""
    case ":${PATH:-}:" in
        *":$bin_dir:"*) return ;;
    esac
    if [ "$modify_path" = yes ]; then
        shell_name=${SHELL:-sh}
        case "${shell_name##*/}" in
            zsh)
                add_to_profile "${ZDOTDIR:-$HOME}/.zshrc"
                ;;
            bash)
                add_to_profile "$HOME/.bashrc"
                # Login Bash reads only the first of these files that exists.
                if [ -f "$HOME/.bash_profile" ]; then
                    add_to_profile "$HOME/.bash_profile"
                elif [ -f "$HOME/.bash_login" ]; then
                    add_to_profile "$HOME/.bash_login"
                else
                    add_to_profile "$HOME/.profile"
                fi
                ;;
            sh|dash|ksh)
                add_to_profile "$HOME/.profile"
                ;;
            *)
                printf 'Add %s to your shell PATH.\n' "$bin_dir"
                modify_path=no
                ;;
        esac
    fi
    printf '\nFor this terminal (sh/bash/zsh), run:\n  %s\n' "$path_line"
    if [ "$modify_path" = yes ]; then
        printf 'New terminals will use the updated shell configuration.\n'
    fi
}

# Keep the entry point at the end so a piped download is read before installation.
main() {
    prefix=${PREFIX:-"${HOME:?HOME must be set}/.local"}
    binary=
    ref=
    modify_path=yes
    uninstall=no
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --prefix|--binary|--ref)
                [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 requires a value."
                case "$1" in
                    --prefix) prefix=$2 ;;
                    --binary) binary=$2 ;;
                    --ref) ref=$2 ;;
                esac
                shift 2
                ;;
            --no-modify-path) modify_path=no; shift ;;
            --uninstall) uninstall=yes; shift ;;
            --help|-h) usage; return ;;
            *) fail "Unknown option: $1. Use --help." ;;
        esac
    done
    case "$(uname -s)" in
        Linux|Darwin) ;;
        *) fail 'Only Linux and macOS are supported.' ;;
    esac
    case "$prefix" in
        /*) ;;
        *) fail '--prefix must be an absolute path.' ;;
    esac
    case "$prefix" in
        *:*) fail '--prefix cannot contain a colon (a PATH separator).' ;;
        *"
"*) fail '--prefix cannot contain a newline.' ;;
    esac
    while [ "${prefix%/}" != "$prefix" ]; do prefix=${prefix%/}; done
    [ -n "$prefix" ] || fail '--prefix cannot be the filesystem root.'
    bin_dir="$prefix/bin"
    lib_dir="$prefix/libexec/zero-code"
    launcher="$bin_dir/zero-code"
    installed_binary="$lib_dir/zero-code"

    if [ "$uninstall" = yes ]; then
        rm -f "$launcher" "$installed_binary"
        rmdir "$lib_dir" 2>/dev/null || :
        printf 'Removed zero-code from %s. Shell PATH entries were kept.\n' "$prefix"
        return
    fi
    [ -z "$binary" ] || [ -z "$ref" ] || fail '--binary and --ref cannot be combined.'

    temp_dir=
    staged_binary=
    staged_launcher=
    trap 'if [ -n "$staged_binary" ]; then rm -f "$staged_binary"; fi; if [ -n "$staged_launcher" ]; then rm -f "$staged_launcher"; fi; if [ -n "$temp_dir" ]; then rm -rf "$temp_dir"; fi' 0
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [ -z "$binary" ]; then
        require cc
        require git
        project_dir=
        if [ -z "$ref" ] && [ -f "$0" ]; then
            candidate=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
            if [ -f "$candidate/zero.toml" ] && [ -f "$candidate/zero.graph" ] && [ -f "$candidate/scripts/build.sh" ]; then
                project_dir=$candidate
            fi
        fi
        if [ -z "$project_dir" ]; then
            require curl
            require tar
            temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/zero-code-install.XXXXXX")
            project_dir="$temp_dir/source"
            mkdir "$project_dir"
            ref=${ref:-main}
            printf 'Downloading zero-code (%s)…\n' "$ref"
            curl --fail --location --show-error --retry 3 \
                "https://codeload.github.com/Protocol-Lattice/ZeroCode/tar.gz/$ref" \
                -o "$temp_dir/source.tar.gz"
            tar -xzf "$temp_dir/source.tar.gz" --strip-components=1 -C "$project_dir"
        fi
        compiler=${ZERO_COMPILER:-"$project_dir/.tools/bin/zero"}
        if [ -z "${ZERO_COMPILER:-}" ]; then
            if [ ! -x "$compiler" ] || [ ! -f "$project_dir/.tools/compiler-frame-limit" ] || [ "$(cat "$project_dir/.tools/compiler-frame-limit")" != 16777216 ]; then
                sh "$project_dir/scripts/setup-zero.sh"
            fi
        fi
        sh "$project_dir/scripts/build.sh"
        binary="$project_dir/dist/zero-code"
    fi
    [ -f "$binary" ] && [ -x "$binary" ] || fail "Not an executable binary: $binary"
    binary=$(CDPATH= cd -- "$(dirname -- "$binary")" && pwd)/$(basename -- "$binary")
    # Detect a wrong-platform or broken build before replacing an existing install.
    "$binary" --version || fail 'The binary cannot run on this machine.'
    [ ! -d "$launcher" ] && [ ! -d "$installed_binary" ] || fail 'An installation destination is a directory.'
    mkdir -p "$bin_dir" "$lib_dir"
    staged_binary=$(mktemp "$lib_dir/.zero-code.XXXXXX")
    cp "$binary" "$staged_binary"
    chmod 755 "$staged_binary"
    staged_launcher=$(mktemp "$bin_dir/.zero-code.XXXXXX")
    {
        printf '#!/bin/sh\n# Installed by zero-code-tui/install.sh.\n'
        # The app re-executes argv[0] for HTTP requests. An absolute path also
        # keeps --cwd and invocation via PATH working without changing directory.
        printf 'exec %s "$@"\n' "$(shell_quote "$installed_binary")"
    } > "$staged_launcher"
    chmod 755 "$staged_launcher"
    mv -f "$staged_binary" "$installed_binary"
    staged_binary=
    mv -f "$staged_launcher" "$launcher"
    staged_launcher=
    printf '\nInstalled %s\n' "$launcher"
    configure_path
    printf '\nRun zero-code from any project directory.\n'
}

main "$@"
