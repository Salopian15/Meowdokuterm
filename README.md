# meowdokuterm

A cat-placement logic puzzle for the terminal (Linux and macOS).

**Rules:** put exactly one cat in every row, every column and every coloured region.
Cats are territorial, so no two cats may touch, not even diagonally.
Every puzzle has exactly one solution, and a wrong cat costs one of your 3 lives.

## Install (no root, nothing to download)

It's a single Python 3.6+ file that uses only the standard library (`curses`).
The file and command are called **`meshcheck`**, so on a shared cluster `ps`/`top`
just shows something that looks like a mesh-validation tool.

```sh
sh install.sh          # copies it to ~/.local/bin/meshcheck
meshcheck
```

Or just run `python3 meshcheck.py`. If your cluster's default `python3` was built
without curses, try `/usr/bin/python3`, `module load python`, or a conda Python.

## Playing

`meshcheck` opens a menu:

* **Play:** a fresh random puzzle at the chosen size and difficulty.
* **Levels:** 20 numbered levels for every size (5×5 to 12×12) and difficulty.
  Levels are the same on every machine, solved ones get a ✓ and your best time,
  and `n` after a win takes you to the next level.
* **Daily puzzle:** the same 8×8 puzzle for everyone today.
* **Board size / Difficulty / Lives:** change with ←/→.
* **Stats** and **How to play**.

Use the arrow keys (or `hjkl`) and Enter, or click with the mouse. In a puzzle, `m` or
Esc goes back to the menu. Tab (stealth) and `b` (boss key) work on every screen.

You can also skip the menu:

```sh
meshcheck -s 10 -d hard            # preselect size 4-14 and easy / medium / hard / any
meshcheck --play                   # straight into a random puzzle
meshcheck --level 3 -s 8 -d hard   # straight into level 3 of the 8x8 hard levels
meshcheck --seed 1234              # reproducible puzzle (seed shown in the header)
meshcheck --daily                  # today's daily puzzle
meshcheck --stealth                # start in stealth mode
meshcheck --lives 0                # no lives limit
meshcheck --stats                  # best times and level progress
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
| `n` / `r` | next puzzle (or level) / restart |
| `m` / Esc | back to the menu |
| `q` | quit |

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
meshcheck --print 20 -s 9 -d hard > hard9.txt              # 20 puzzles as text
meshcheck --print 5 -s 8 --solutions                       # with answer keys
meshcheck --load hard9.txt --index 3                       # play from a file (n = next)
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

Stats and level progress are saved in `~/.local/share/meshcheck/stats.json`
(stats from the old `purrgrid` name are picked up automatically).
