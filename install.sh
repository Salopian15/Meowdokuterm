#!/bin/sh
# Installs meowdokuterm (as the `meshcheck` command) into ~/.local/bin (no root needed).
#
#   sh install.sh               install (or update)
#   sh install.sh --uninstall   remove the command (your stats are kept)
#   PYTHON=/path/to/python3 sh install.sh    use a specific Python
#   PREFIX=/some/dir sh install.sh           install into /some/dir/bin
set -e
dest="${PREFIX:-$HOME/.local}/bin"
target="$dest/meshcheck"
here="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}/meshcheck"

if [ "$1" = "--uninstall" ]; then
    if [ -e "$target" ]; then
        rm -f "$target"
        echo "Removed $target"
    else
        echo "Nothing to remove: $target does not exist"
    fi
    [ -d "$data" ] && echo "Your stats and saved game are still in $data (delete it by hand if you like)."
    exit 0
fi

if [ ! -f "$here/meshcheck.py" ]; then
    echo "error: $here/meshcheck.py not found; run this script from the project folder." >&2
    exit 1
fi

# Python 3.6+ with the curses module. Cluster default pythons often lack curses.
good_python() {
    "$1" -c 'import sys, curses; sys.exit(sys.version_info < (3, 6))' >/dev/null 2>&1
}

py=""
for cand in ${PYTHON:+"$PYTHON"} python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 \
            python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 python3.6 python; do
    path="$(command -v "$cand" 2>/dev/null)" || continue
    if good_python "$path"; then
        py="$path"
        break
    fi
done

if [ -z "$py" ]; then
    echo "error: couldn't find a Python 3.6+ with the curses module." >&2
    echo "  Try 'module load python' or activating conda, or point at one:" >&2
    echo "  PYTHON=/path/to/python3 sh install.sh" >&2
    exit 1
fi

mkdir -p "$dest"
tmp="$target.tmp.$$"
# swap the generic shebang for the Python that we know has curses
{ printf '#!%s\n' "$py"; tail -n +2 "$here/meshcheck.py"; } > "$tmp"
chmod +x "$tmp"
mv -f "$tmp" "$target"

if ! "$target" --version >/dev/null 2>&1; then
    echo "error: installed $target but it doesn't run; try PYTHON=/path/to/python3 sh install.sh" >&2
    exit 1
fi
echo "Installed $target (using $py)"

case ":$PATH:" in
    *":$dest:"*) ;;
    *) echo "Add this to your ~/.bashrc or ~/.zshrc:  export PATH=\"$dest:\$PATH\"" ;;
esac
