# purrgrid

A cat-placement logic puzzle for the terminal (Linux and macOS).

**Rules:** put exactly one cat in every row, every column and every coloured region.
Cats are territorial, so no two cats may touch, not even diagonally.
Every puzzle has exactly one solution, and a wrong cat costs one of your 3 lives.

## Install (no root, nothing to download)

It's a single Python 3.6+ file that uses only the standard library (`curses`).

```sh
sh install.sh          # copies it to ~/.local/bin/purrgrid
purrgrid
```

Or just run `python3 purrgrid.py`. If your cluster's default `python3` was built
without curses, try `/usr/bin/python3`, `module load python`, or a conda Python.

## Playing

```sh
purrgrid                       # 8x8, medium
purrgrid -s 10 -d hard         # sizes 4-14; easy / medium / hard / any
purrgrid --seed 1234           # reproducible puzzle (seed shown in the header)
purrgrid --daily               # same 8x8 puzzle for everyone today
purrgrid --stealth             # start in stealth mode
purrgrid --lives 0             # no lives limit
purrgrid --stats               # best times
```

| Key | Action |
| --- | --- |
| arrows / `hjkl` | move |
| `c` / Enter | place cat |
| `x` / `.` | toggle a mark (free note) |
| space | cycle empty → mark → cat |
| mouse | left-click = cat, right-click = mark |
| `u` / `C` | undo / clear all marks |
| `H` | hint (places one correct cat) |
| `a` | auto-mark cells ruled out by a new cat (on by default) |
| `B` / `L` | region borders / region letters |
| **Tab** | **stealth mode** |
| **`b`** | **boss key** |
| `n` / `r` / `q` | new puzzle / restart / quit |

### Not looking like you're skiving

* **Stealth mode (Tab):** the board is shown as an IPython session printing a numpy
  `masked_array`. Region numbers are the values, marks are `--`, cats are `-1`, the cursor
  is a highlighted entry, and mistakes appear as `RuntimeWarning`s. It's monochrome,
  so regions are told apart by number.
* **Boss key (`b`):** switches instantly to a fake `tail -f` of a training job's slurm
  log that keeps scrolling with live timestamps. The timer pauses. Only `b` brings the
  game back, so hitting other keys does nothing visible.

## Level generator

```sh
purrgrid --print 20 -s 9 -d hard > hard9.txt              # 20 puzzles as text
purrgrid --print 5 -s 8 --solutions                       # with answer keys
purrgrid --load hard9.txt --index 3                       # play from a file (n = next)
```

The file format is one letter per region, with puzzles separated by blank lines, so
you can write your own puzzles by hand. `--load` solves and rates them.

Difficulty is rated by solving like a person would:

* **easy:** forced cells, plus "every option for this row, column or region rules that cell out".
* **medium:** also needs confinement: k regions that fit inside k rows or columns
  (or the reverse) take those lines over.
* **hard:** also needs "if a cat went here, things break" chains.

Generation takes under a second for 8×8 up to 10×10. 12×12 and larger can take
several seconds.

Stats are saved in `~/.local/share/purrgrid/stats.json`.
