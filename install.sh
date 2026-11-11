#!/bin/sh
# Installs purrgrid into ~/.local/bin (no root needed).
set -e
dest="${PREFIX:-$HOME/.local}/bin"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$dest"
cp "$here/purrgrid.py" "$dest/purrgrid"
chmod +x "$dest/purrgrid"
echo "Installed $dest/purrgrid"
if ! python3 -c "import curses" 2>/dev/null; then
    echo "warning: your python3 has no curses module; try 'module load python' or conda." >&2
fi
case ":$PATH:" in
    *":$dest:"*) ;;
    *) echo "Add this to your ~/.bashrc or ~/.zshrc:  export PATH=\"$dest:\$PATH\"" ;;
esac
