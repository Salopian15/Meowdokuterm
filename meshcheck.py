#!/usr/bin/env python3
"""
meowdokuterm - a cat-placement logic puzzle for the terminal.

The file and command are called `meshcheck` so it looks like an ordinary
mesh-validation tool in `ps`/`top` on a shared cluster.

Rules: place exactly one cat in every row, every column and every coloured
region. Cats are territorial: no two cats may touch, not even diagonally.
Every generated puzzle has exactly one solution. A wrong cat costs a life.

Pure Python 3.6+ standard library (curses). No installs needed.
Run `meshcheck --help` for options, press `?` in game for keys.
"""

import argparse
import datetime
import json
import locale
import os
import random
import sys
import time

try:
    import curses
except ImportError:  # e.g. Windows, or a Python built without ncurses
    curses = None

VERSION = "1.0"
DIFFICULTIES = ("easy", "medium", "hard")
LETTERS = "abcdefghijklmnopqrstuvwxyz"
MIN_N, MAX_N = 4, 14

# Levels: a fixed, numbered set of puzzles for each board size and difficulty.
LEVEL_COUNT = 20
LEVEL_SIZES = tuple(range(5, 13))
LEVEL_SEED_BASE = 10 ** 6  # random quick-play seeds stay below this

EMPTY, MARK, CAT = 0, 1, 2


# ---------------------------------------------------------------------------
# Puzzle core: geometry, exact solver, logical difficulty rater, generator
# ---------------------------------------------------------------------------

def popcount(x):
    return bin(x).count("1")


def iter_bits(x):
    while x:
        low = x & -x
        yield low.bit_length() - 1
        x ^= low


class Geometry(object):
    """Bitmask view of a board. Units 0..n-1 rows, n..2n-1 cols, 2n..3n-1 regions."""

    def __init__(self, n, regions):
        self.n = n
        self.regions = regions
        self.full = (1 << (n * n)) - 1
        rows, cols, regs = [0] * n, [0] * n, [0] * n
        for r in range(n):
            for c in range(n):
                b = 1 << (r * n + c)
                rows[r] |= b
                cols[c] |= b
                regs[regions[r][c]] |= b
        self.units = rows + cols + regs
        self.cell_units = []
        self.kill = []
        for r in range(n):
            for c in range(n):
                reg = regions[r][c]
                self.cell_units.append((r, n + c, 2 * n + reg))
                m = rows[r] | cols[c] | regs[reg]
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        rr, cc = r + dr, c + dc
                        if 0 <= rr < n and 0 <= cc < n:
                            m |= 1 << (rr * n + cc)
                self.kill.append(m & ~(1 << (r * n + c)))


def solve_all(g, limit=2):
    """Exact solver. Returns up to `limit` solutions as sorted cell lists."""
    out = []

    def rec(avail, unsat, placed):
        if len(out) >= limit:
            return
        if not unsat:
            out.append(sorted(placed))
            return
        best, best_count = None, 1 << 30
        for u in unsat:
            k = popcount(avail & g.units[u])
            if k < best_count:
                best, best_count = u, k
                if k == 0:
                    return
        for cell in iter_bits(avail & g.units[best]):
            rec(avail & ~g.kill[cell] & ~(1 << cell),
                unsat.difference(g.cell_units[cell]), placed + [cell])

    rec(g.full, frozenset(range(3 * g.n)), [])
    return out


def _basic_step(g, avail, unsat):
    """One easy deduction. Returns (avail, changed) or (None, _) on contradiction.

    T1: a row/col/region with a single possible cell gets its cat.
    T2: a cell ruled out by *every* option of some row/col/region is removed
        (covers "region stuck in one row", "cells next to all options", ...).
    """
    for u in unsat:
        m = avail & g.units[u]
        if m == 0:
            return None, False
        if m & (m - 1) == 0:
            cell = m.bit_length() - 1
            unsat.difference_update(g.cell_units[cell])
            return avail & ~g.kill[cell] & ~(1 << cell), True
    for u in unsat:
        inter = g.full
        for cell in iter_bits(avail & g.units[u]):
            inter &= g.kill[cell]
        inter &= avail
        if inter:
            return avail & ~inter, True
    return avail, False


def _propagate(g, avail, unsat):
    while unsat:
        avail, changed = _basic_step(g, avail, unsat)
        if avail is None or not changed:
            return avail
    return avail


def _confinement_step(g, avail, unsat, kmax=4):
    """k units of one kind whose options lie inside exactly k units of another
    kind own those k units: remove everything else from them."""
    n = g.n
    kinds = [[u for u in unsat if t * n <= u < (t + 1) * n] for t in range(3)]
    from itertools import combinations
    for a in range(3):
        for b in range(3):
            if a == b or len(kinds[a]) < 3:
                continue
            masks = [(u, avail & g.units[u]) for u in kinds[a]]
            for k in range(2, min(kmax, len(masks) - 1) + 1):
                for combo in combinations(masks, k):
                    union = 0
                    for _, m in combo:
                        union |= m
                    touched = [v for v in kinds[b] if avail & g.units[v] & union]
                    if len(touched) == k:
                        other = 0
                        for v in touched:
                            other |= g.units[v]
                        kill = avail & other & ~union
                        if kill:
                            return avail & ~kill
    return None


def _lookahead_step(g, avail, unsat):
    """Try a cat in a cell, follow easy deductions; contradiction => not a cat."""
    for x in iter_bits(avail):
        u2 = set(unsat)
        u2.difference_update(g.cell_units[x])
        res = _propagate(g, avail & ~g.kill[x] & ~(1 << x), u2)
        if res is None:
            return avail & ~(1 << x)
    return None


def rate(g):
    """Solve with human-style techniques; return 'easy'/'medium'/'hard'.

    easy   : only T1/T2 (see _basic_step)
    medium : also needs confinement of 2-4 regions/rows/cols
    hard   : also needs "what if a cat went here?" chains
    """
    avail = g.full
    unsat = set(range(3 * g.n))
    level = 0
    while unsat:
        avail, changed = _basic_step(g, avail, unsat)
        if avail is None:
            return "hard"
        if changed:
            continue
        nxt = _confinement_step(g, avail, unsat)
        if nxt is not None:
            level = max(level, 1)
            avail = nxt
            continue
        nxt = _lookahead_step(g, avail, unsat)
        if nxt is not None:
            level = 2
            avail = nxt
            continue
        return "hard"
    return DIFFICULTIES[level]


def random_solution(n, rng):
    """Random permutation with no two consecutive rows' cats touching."""
    def bt(p, used):
        if len(p) == n:
            return p
        cand = list(range(n))
        rng.shuffle(cand)
        for c in cand:
            if c in used or (p and abs(c - p[-1]) < 2):
                continue
            res = bt(p + [c], used | {c})
            if res:
                return res
        return None
    return bt([], frozenset())


NEIGH4 = ((-1, 0), (1, 0), (0, -1), (0, 1))


def grow_regions(n, sol, rng):
    ids = list(range(n))
    rng.shuffle(ids)
    reg = [[-1] * n for _ in range(n)]
    for r, c in enumerate(sol):
        reg[r][c] = ids[r]
    left = n * n - n
    while left:
        opts = []
        for r in range(n):
            for c in range(n):
                if reg[r][c] != -1:
                    continue
                for dr, dc in NEIGH4:
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < n and 0 <= cc < n and reg[rr][cc] != -1:
                        opts.append((r, c, reg[rr][cc]))
        r, c, k = rng.choice(opts)
        reg[r][c] = k
        left -= 1
    return reg


def region_connected_without(reg, n, k, skip):
    cells = set((r, c) for r in range(n) for c in range(n)
                if reg[r][c] == k and (r, c) != skip)
    if not cells:
        return False
    start = next(iter(cells))
    seen, stack = {start}, [start]
    while stack:
        r, c = stack.pop()
        for dr, dc in NEIGH4:
            q = (r + dr, c + dc)
            if q in cells and q not in seen:
                seen.add(q)
                stack.append(q)
    return len(seen) == len(cells)


class Puzzle(object):
    def __init__(self, n, regions, solution, seed=None, difficulty=None, label=""):
        self.n = n
        self.regions = regions
        self.solution = solution  # solution[row] = col
        self.seed = seed
        self.difficulty = difficulty
        self.label = label
        self.geom = Geometry(n, regions)


def generate_unique(n, rng, max_fix=400):
    """Generate regions with exactly one solution (repairs ambiguities)."""
    while True:
        sol = random_solution(n, rng)
        target = sorted(r * n + c for r, c in enumerate(sol))
        tset = set(target)
        reg = grow_regions(n, sol, rng)
        for _ in range(max_fix):
            sols = solve_all(Geometry(n, reg), 2)
            if len(sols) == 1:
                return reg, sol
            other = next(s for s in sols if s != target)
            cands = [x for x in other if x not in tset]
            rng.shuffle(cands)
            moved = False
            for x in cands:
                r, c = divmod(x, n)
                old = reg[r][c]
                nb = sorted(set(reg[r + dr][c + dc] for dr, dc in NEIGH4
                                if 0 <= r + dr < n and 0 <= c + dc < n) - {old})
                if not nb or not region_connected_without(reg, n, old, (r, c)):
                    continue
                reg[r][c] = rng.choice(nb)
                moved = True
                break
            if not moved:
                break


def generate(n, difficulty="medium", seed=None, max_tries=600, budget=None):
    """Generate a unique puzzle of the wanted difficulty (closest match if the
    time budget runs out; hard boards are rare, especially on small grids)."""
    if budget is None:
        budget = 6.0 if n <= 10 else 20.0
    if seed is None:
        seed = random.randrange(10 ** 6)
    rng = random.Random("%d-%s-%d" % (n, difficulty, seed))
    best = None
    want = DIFFICULTIES.index(difficulty) if difficulty in DIFFICULTIES else None
    deadline = time.time() + budget
    for i in range(max_tries):
        if i and time.time() > deadline:
            break
        reg, sol = generate_unique(n, rng)
        d = rate(Geometry(n, reg))
        if want is None or d == difficulty:
            return Puzzle(n, reg, sol, seed, d)
        dist = abs(DIFFICULTIES.index(d) - want)
        if best is None or dist < best[0]:
            best = (dist, reg, sol, d)
    _, reg, sol, d = best
    return Puzzle(n, reg, sol, seed, d)


def level_puzzle(n, difficulty, level):
    """Level `level` (1-based) of a pack. No time budget, so every machine
    generates exactly the same board."""
    p = generate(n, difficulty, LEVEL_SEED_BASE + level, max_tries=300, budget=float("inf"))
    p.label = "level %d" % level
    p.level = (n, difficulty, level)
    return p


def daily_puzzle(day=None):
    day = day or datetime.date.today()
    seed = int(day.strftime("%Y%m%d"))
    p = generate(8, "medium", seed)
    p.label = "daily " + day.isoformat()
    return p


# ---------------------------------------------------------------------------
# Text format (for --print / --load)
# ---------------------------------------------------------------------------

def puzzle_to_text(p, with_solution=False):
    lines = ["# meshcheck size=%d difficulty=%s seed=%s" % (p.n, p.difficulty, p.seed)]
    for r in range(p.n):
        lines.append("".join(LETTERS[p.regions[r][c]] for c in range(p.n)))
    if with_solution:
        lines.append("# solution (column per row, 0-based): " +
                     " ".join(str(c) for c in p.solution))
        for r in range(p.n):
            lines.append("#   " + "".join(
                "*" if p.solution[r] == c else LETTERS[p.regions[r][c]]
                for c in range(p.n)))
    return "\n".join(lines)


def parse_puzzles(text):
    puzzles, block, meta = [], [], {}

    def flush():
        if not block:
            return
        n = len(block)
        if any(len(row) != n for row in block):
            raise ValueError("puzzle is not square:\n" + "\n".join(block))
        ids = {}
        regs = [[ids.setdefault(ch, len(ids)) for ch in row] for row in block]
        if len(ids) != n:
            raise ValueError("a %dx%d puzzle needs %d regions, found %d" % (n, n, n, len(ids)))
        sols = solve_all(Geometry(n, regs), 2)
        if not sols:
            raise ValueError("puzzle has no solution:\n" + "\n".join(block))
        sol = [0] * n
        for x in sols[0]:
            sol[x // n] = x % n
        p = Puzzle(n, regs, sol, meta.get("seed"), meta.get("difficulty") or rate(Geometry(n, regs)))
        p.ambiguous = len(sols) > 1
        puzzles.append(p)

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            for tok in line[1:].split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    meta[k] = v
            continue
        if not line:
            flush()
            block, meta = [], {}
            continue
        block.append(line)
    flush()
    return puzzles


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats_path(name="meshcheck"):
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, name, "stats.json")


def load_stats():
    # fall back to stats saved under the project's old name (purrgrid)
    for path in (stats_path(), stats_path("purrgrid")):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def level_key(n, difficulty):
    return "%dx%d-%s" % (n, n, difficulty)


def level_progress(stats, n, difficulty):
    """{level number: best time or None (solved with hints)} for one level pack."""
    pack = stats.get("levels", {}).get(level_key(n, difficulty), {})
    return dict((int(k), v.get("best")) for k, v in pack.items())


def stats_lines(st):
    lines = []
    for k in sorted(st):
        if k == "daily":
            lines.append("daily puzzles solved: %d" % len(st[k]))
        elif k == "levels":
            for pack in sorted(st[k], key=lambda s: (int(s.split("x")[0]), s)):
                lines.append("levels %-14s %2d/%d solved" % (pack, len(st[k][pack]), LEVEL_COUNT))
        else:
            best = st[k]["best"]
            lines.append("%-14s solved %4d   best %s" % (k, st[k]["solved"],
                                                       fmt_time(best) if best is not None else "-"))
    return lines


def fmt_time(t):
    t = int(t)
    return "%d:%02d:%02d" % (t // 3600, t // 60 % 60, t % 60) if t >= 3600 else "%02d:%02d" % (t // 60, t % 60)


def save_stats(st):
    try:
        path = stats_path()
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            os.makedirs(d)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------

class Game(object):
    def __init__(self, puzzle, lives):
        self.p = puzzle
        n = puzzle.n
        self.grid = [[EMPTY] * n for _ in range(n)]
        self.cur = [0, 0]
        self.max_lives = lives
        self.lives = lives
        self.undo_stack = []
        self.hints = 0
        self.mistakes = 0
        self.state = "play"  # play / won / lost
        self.t0 = time.time()
        self.t_end = None
        self.paused_at = None
        self.paused_total = 0.0

    def elapsed(self):
        end = self.t_end or self.paused_at or time.time()
        return max(0.0, end - self.t0 - self.paused_total)

    def pause(self):
        if self.paused_at is None and self.state == "play":
            self.paused_at = time.time()

    def resume(self):
        if self.paused_at is not None:
            self.paused_total += time.time() - self.paused_at
            self.paused_at = None

    def snapshot(self):
        self.undo_stack.append([row[:] for row in self.grid])
        if len(self.undo_stack) > 500:
            self.undo_stack.pop(0)

    def undo(self):
        if self.undo_stack and self.state == "play":
            self.grid = self.undo_stack.pop()
            return True
        return False

    def cats(self):
        return sum(row.count(CAT) for row in self.grid)

    def toggle_mark(self, r, c):
        if self.state != "play" or self.grid[r][c] == CAT:
            return
        self.snapshot()
        self.grid[r][c] = MARK if self.grid[r][c] == EMPTY else EMPTY

    def place_cat(self, r, c, automark):
        """Returns 'ok', 'wrong', 'noop', 'won' or 'lost'."""
        if self.state != "play" or self.grid[r][c] == CAT:
            return "noop"
        n = self.p.n
        if self.p.solution[r] == c:
            self.snapshot()
            self.grid[r][c] = CAT
            if automark:
                for x in iter_bits(self.p.geom.kill[r * n + c]):
                    rr, cc = divmod(x, n)
                    if self.grid[rr][cc] == EMPTY:
                        self.grid[rr][cc] = MARK
            if self.cats() == n:
                self.state = "won"
                self.t_end = time.time()
                return "won"
            return "ok"
        self.mistakes += 1
        self.grid[r][c] = MARK
        if self.max_lives > 0:
            self.lives -= 1
            if self.lives <= 0:
                self.state = "lost"
                self.t_end = time.time()
                return "lost"
        return "wrong"

    def hint(self, automark):
        """Place a correct cat in the row/col/region with the fewest open cells."""
        if self.state != "play":
            return None
        n, g = self.p.n, self.p.geom
        open_mask = 0
        for r in range(n):
            for c in range(n):
                if self.grid[r][c] == EMPTY:
                    open_mask |= 1 << (r * n + c)
        best = None
        for r in range(n):
            c = self.p.solution[r]
            if self.grid[r][c] == CAT:
                continue
            x = r * n + c
            score = min(popcount(open_mask & g.units[u]) for u in g.cell_units[x])
            if best is None or score < best[0]:
                best = (score, r, c)
        if best is None:
            return None
        self.hints += 1
        _, r, c = best
        self.cur = [r, c]
        return (r, c), self.place_cat(r, c, automark)


# ---------------------------------------------------------------------------
# Puzzle source
# ---------------------------------------------------------------------------

class FileSource(object):
    """Puzzles loaded with --load, played in order (wrapping around)."""

    def __init__(self, path, index=None):
        self.path = path
        with open(path) as f:
            self.puzzles = parse_puzzles(f.read())
        if not self.puzzles:
            raise SystemExit("no puzzles found in " + path)
        self.idx = (index - 1) - 1 if index else -1

    def next(self):
        self.idx = (self.idx + 1) % len(self.puzzles)
        p = self.puzzles[self.idx]
        p.label = "%s #%d/%d" % (os.path.basename(self.path), self.idx + 1, len(self.puzzles))
        return p


# ---------------------------------------------------------------------------
# Curses UI
# ---------------------------------------------------------------------------

RULES = [
    "Put one cat in every row, every column and every coloured region.",
    "Cats may not touch each other, not even diagonally.",
    "A wrong cat costs a life. Marks are free notes.",
]

HELP_KEYS = [
    ("arrows  hjkl", "move"),
    ("c  Enter", "place a cat"),
    ("x  .", "mark / unmark a cell"),
    ("space", "cycle: empty, mark, cat"),
    ("mouse", "left-click cat, right-click mark"),
    ("u", "undo"),
    ("C", "clear all marks"),
    ("H", "hint (places one cat)"),
    ("a", "auto-mark cells a new cat rules out"),
    ("B  L", "region borders  /  region letters"),
    ("Tab", "stealth mode (IPython look-alike)"),
    ("b", "boss key (press b again to return)"),
    ("n  r", "next puzzle  /  restart"),
    ("m  Esc", "back to the menu"),
    ("q", "quit"),
]

MENU_ITEMS = ("play", "levels", "daily", "size", "difficulty", "lives", "stats", "help", "quit")
MENU_SETTINGS = ("size", "difficulty", "lives")
QUICK_DIFFICULTIES = DIFFICULTIES + ("any",)
LIVES_CHOICES = (1, 2, 3, 5, 0)  # 0 = unlimited
LEVEL_COLS = 5

HELP = (["How to play"] + ["  " + s for s in RULES] + ["", "Keys"] +
        ["  %-14s %s" % kv for kv in HELP_KEYS] + ["", "Press any key to return."])

# Pastel region backgrounds (xterm-256), picked to stay distinct side by side.
# The first 8 are the most distinct; small boards only use those.
PAL256 = [217, 153, 229, 151, 183, 215, 159, 250, 180, 147, 193, 210, 116, 223]
PAL8 = [1, 2, 3, 4, 5, 6, 7]  # standard curses red..white

# Box-drawing junctions keyed by (up, down, left, right).
BOX = {
    (0, 0, 0, 0): u" ", (1, 1, 0, 0): u"│", (0, 0, 1, 1): u"─",
    (0, 1, 0, 1): u"┌", (0, 1, 1, 0): u"┐", (1, 0, 0, 1): u"└",
    (1, 0, 1, 0): u"┘", (1, 1, 0, 1): u"├", (1, 1, 1, 0): u"┤",
    (0, 1, 1, 1): u"┬", (1, 0, 1, 1): u"┴", (1, 1, 1, 1): u"┼",
    (1, 0, 0, 0): u"╵", (0, 1, 0, 0): u"╷", (0, 0, 1, 0): u"╴",
    (0, 0, 0, 1): u"╶",
}
ROUND = {(0, 1, 0, 1): u"╭", (0, 1, 1, 0): u"╮",
         (1, 0, 0, 1): u"╰", (1, 0, 1, 0): u"╯"}


def ascii_box(key):
    u, d, l, r = key
    if not any(key):
        return " "
    if (u or d) and not (l or r):
        return "|"
    if (l or r) and not (u or d):
        return "-"
    return "+"


GLYPHS = {
    # cat must be exactly 3 terminal columns wide (emoji are 2 columns)
    "emoji": dict(cat=u" \U0001F431", mark=u" × ", empty=u" · ", heart=u"♥",
                  heart_off=u"♡", on=u"●", off=u"○", sep=u" · "),
    "face": dict(cat=u"=ω=", mark=u" × ", empty=u" · ", heart=u"♥",
                 heart_off=u"♡", on=u"●", off=u"○", sep=u" · "),
    "ascii": dict(cat="=^=", mark=" x ", empty=" . ", heart="<3", heart_off="--",
                  on="#", off=".", sep=" | "),
}

# colour pair numbers
P_REGION = 1      # 1..14 region fills
P_LINE = 30
P_RED = 31
P_GREEN = 32
P_YELLOW = 33
P_CURSOR = 34
P_FLASH_BAD = 35
P_FLASH_GOOD = 36
P_DIM = 37
P_TITLE = 38


def xterm_rgb(i):
    """Approximate RGB of an xterm-256 colour index."""
    if i < 16:
        base = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200),
                (188, 63, 188), (17, 168, 205), (229, 229, 229)]
        return base[i % 8]
    if i < 232:
        i -= 16
        lv = (0, 95, 135, 175, 215, 255)
        return (lv[i // 36], lv[i // 6 % 6], lv[i % 6])
    v = 8 + (i - 232) * 10
    return (v, v, v)


class FakeLog(object):
    """Endless, plausible-looking training-job log for the boss key."""

    def __init__(self):
        self.rng = random.Random()
        self.user = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"
        self.job = self.rng.randint(4000000, 9999999)
        self.step = self.rng.randint(20000, 90000)
        self.epoch = self.step // 3125
        self.loss = self.rng.uniform(1.6, 2.4)
        self.lines = ["$ tail -f logs/slurm-%d.out" % self.job]
        for _ in range(60):
            self.lines.append(self.make())
        self.next_at = 0.0

    def make(self):
        rng = self.rng
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.step += 10
        if self.step % 3125 < 10:
            self.epoch += 1
        self.loss = max(0.05, self.loss * rng.uniform(0.9985, 1.0012))
        roll = rng.random()
        if roll < 0.03:
            return ("[%s] INFO  ckpt  | saved /scratch/%s/runs/r%d/step_%07d.pt (%.1f GB) in %.1fs"
                    % (ts, self.user, self.job % 1000, self.step, rng.uniform(5.8, 6.1), rng.uniform(8, 20)))
        if roll < 0.08:
            return ("[%s] INFO  eval  | step %7d | val_loss %.4f | ppl %.2f"
                    % (ts, self.step, self.loss * 1.04, 2.718281828 ** (self.loss * 1.04)))
        if roll < 0.14:
            return ("[%s] DEBUG nccl  | rank %d allreduce bucket %d: %.2f ms"
                    % (ts, rng.randint(0, 31), rng.randint(0, 47), rng.uniform(3, 14)))
        return ("[%s] INFO  train | epoch %3d | step %7d | loss %.4f | lr %.2e | %6.0f tok/s | mem %.1fG"
                % (ts, self.epoch, self.step, self.loss, 3e-4 * (1 - self.step / 4e5),
                   rng.uniform(41000, 43800), rng.uniform(61.2, 63.9)))

    def tick(self):
        now = time.time()
        if now >= self.next_at:
            for _ in range(self.rng.choice((1, 1, 1, 2, 3))):
                self.lines.append(self.make())
            self.lines = self.lines[-400:]
            self.next_at = now + self.rng.uniform(0.4, 2.2)


class App(object):
    def __init__(self, scr, args, file_source=None):
        self.scr = scr
        self.args = args
        self.file_source = file_source
        self.size = args.size
        self.difficulty = args.difficulty
        self.lives = args.lives
        self.screen = "menu"  # menu / levels / stats / game
        self.mode = None      # what "next puzzle" means: quick / daily / file / level
        self.game = None
        self.menu_sel = 0
        self.lv_focus = 2     # 0 size row, 1 difficulty row, 2 the level grid
        self.lv_sel = 0
        self.hits = []        # clickable menu areas: (y, x, width, callback)
        self.stealth = args.stealth
        self.automark = not args.no_automark
        self.letters = args.letters
        self.borders = not args.compact
        self.help = False
        self.boss = None
        self.confirm = None
        self.msg = ("", "")
        self.msg_kind = "info"
        self.flash = None  # (r, c, kind, until)
        self.cellpos = {}
        self.stats = load_stats()
        self.in_no = random.randint(17, 64)
        enc = (locale.getpreferredencoding() or "").lower()
        self.uni = "utf" in enc and not args.ascii
        style = args.cat if self.uni else "ascii"
        self.gl = GLYPHS[style]
        self.setup_curses()
        self.say("", "")
        if file_source:
            self.start("file")
        elif args.daily:
            self.start("daily")
        elif args.level:
            self.start("level", level=args.level)
        elif args.seed is not None or args.play:
            self.start("quick", seed=args.seed)

    # -- setup ------------------------------------------------------------
    def setup_curses(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(200)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            curses.mouseinterval(0)
        except Exception:
            pass
        self.color = curses.has_colors() and not self.args.no_color
        self.npal = 0
        self.rich = False
        if not self.color:
            return
        try:
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            self.bgdef = bg
            self.pairs = {}
            self.next_pair = 40
            self.rich = curses.COLORS >= 256 and curses.COLOR_PAIRS > 64
            if self.rich:
                self.pal, self.ink = PAL256, 238
                for i, b in enumerate(PAL256):
                    curses.init_pair(P_REGION + i, 238, b)
                self.npal = len(PAL256)
                curses.init_pair(P_LINE, 244, bg)
                curses.init_pair(P_RED, 203, bg)
                curses.init_pair(P_GREEN, 114, bg)
                curses.init_pair(P_YELLOW, 221, bg)
                curses.init_pair(P_CURSOR, 231, 237)
                curses.init_pair(P_FLASH_BAD, 231, 160)
                curses.init_pair(P_FLASH_GOOD, 16, 120)
                curses.init_pair(P_DIM, 245, bg)
                curses.init_pair(P_TITLE, 16, 217)
            else:
                self.pal, self.ink = PAL8, 0
                for i, b in enumerate(PAL8):
                    curses.init_pair(P_REGION + i, 0, b)
                self.npal = len(PAL8)
                curses.init_pair(P_LINE, curses.COLOR_WHITE, bg)
                curses.init_pair(P_RED, curses.COLOR_RED, bg)
                curses.init_pair(P_GREEN, curses.COLOR_GREEN, bg)
                curses.init_pair(P_YELLOW, curses.COLOR_YELLOW, bg)
                curses.init_pair(P_CURSOR, curses.COLOR_WHITE, curses.COLOR_BLACK)
                curses.init_pair(P_FLASH_BAD, curses.COLOR_WHITE, curses.COLOR_RED)
                curses.init_pair(P_FLASH_GOOD, curses.COLOR_BLACK, curses.COLOR_GREEN)
                curses.init_pair(P_DIM, curses.COLOR_WHITE, bg)
                curses.init_pair(P_TITLE, curses.COLOR_BLACK, curses.COLOR_MAGENTA)
        except (curses.error, ValueError):
            self.color = False
            self.npal = 0
            self.rich = False

    def cp(self, n):
        return curses.color_pair(n) if self.color else 0

    def pair(self, fg, bg):
        """Colour pair for (fg, bg), allocated on first use. None = terminal default."""
        if not self.color:
            return 0
        fg = self.bgdef if fg is None else fg
        bg = self.bgdef if bg is None else bg
        key = (fg, bg)
        p = self.pairs.get(key)
        if p is None:
            if self.next_pair >= curses.COLOR_PAIRS:
                return 0
            try:
                curses.init_pair(self.next_pair, fg, bg)
            except (curses.error, ValueError):
                return 0
            p = self.pairs[key] = self.next_pair
            self.next_pair += 1
        return curses.color_pair(p)

    def dim(self):
        return self.cp(P_DIM) if self.rich else curses.A_DIM

    # -- game lifecycle ---------------------------------------------------
    def level_size(self):
        """The current size, clamped to the sizes that have levels."""
        return min(max(self.size, LEVEL_SIZES[0]), LEVEL_SIZES[-1])

    def level_difficulty(self):
        return self.difficulty if self.difficulty in DIFFICULTIES else "medium"

    def first_unsolved(self, n, d):
        done = level_progress(self.stats, n, d)
        for i in range(1, LEVEL_COUNT + 1):
            if i not in done:
                return i
        return 1

    def start(self, mode, seed=None, level=None):
        """Start a puzzle. mode: quick / daily / file / level."""
        self.mode = mode
        if mode == "file":
            puzzle = self.file_source.next()
        elif mode == "daily":
            self.status_only("Fetching today's puzzle...", "Running cells...")
            puzzle = daily_puzzle()
        elif mode == "level":
            n, d = self.level_size(), self.level_difficulty()
            self.size, self.difficulty = n, d
            level = level or self.first_unsolved(n, d)
            self.status_only("Generating level %d (%dx%d %s)..." % (level, n, n, d), "Running cells...")
            puzzle = level_puzzle(n, d, level)
        else:
            self.status_only("Generating a %dx%d %s puzzle..." % (self.size, self.size, self.difficulty),
                             "Running cells...")
            puzzle = generate(self.size, self.difficulty, seed)
        self.new_game(puzzle)

    def next_puzzle(self):
        level = getattr(self.game.p, "level", None) if self.game else None
        if self.mode == "level" and level:
            n, d, i = level
            if i >= LEVEL_COUNT:
                self.open_levels()
                self.say("You've finished every %dx%d %s level!" % (n, n, d), "", kind="good")
                return
            self.start("level", level=i + 1)
        elif self.mode == "file":
            self.start("file")
        else:
            self.start("quick")

    def new_game(self, puzzle):
        self.game = Game(puzzle, self.lives)
        self.screen = "game"
        self.confirm = None
        self.assign_colours(puzzle)
        self.flash = None
        self.in_no += 3
        if getattr(puzzle, "ambiguous", False):
            self.say("Warning: this puzzle has more than one solution.",
                     "UserWarning: system is under-determined", kind="bad")
        else:
            self.say("", "")

    def open_menu(self):
        self.screen = "menu"
        self.game = None
        self.confirm = None
        self.say("", "")

    def leave_game(self):
        """Back to wherever this puzzle was picked from."""
        if self.mode == "level":
            self.open_levels()
        else:
            self.open_menu()

    def open_levels(self):
        self.screen = "levels"
        self.game = None
        self.confirm = None
        self.lv_focus = 2
        self.lv_sel = self.first_unsolved(self.level_size(), self.level_difficulty()) - 1
        self.say("", "")

    def say(self, normal, stealth=None, kind="info"):
        self.msg = (normal, normal if stealth is None else stealth)
        self.msg_kind = kind

    def status_only(self, normal, stealth):
        try:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            if self.stealth:
                self.put(0, 0, stealth)
            else:
                self.put(h // 2, max(0, (w - len(normal)) // 2), normal, self.dim())
            self.scr.refresh()
        except curses.error:
            pass

    def record_win(self):
        g = self.game
        p = g.p
        key = "%dx%d-%s" % (p.n, p.n, p.difficulty)
        st = self.stats.setdefault(key, {"solved": 0, "best": None})
        st["solved"] += 1
        t = round(g.elapsed(), 1)
        new_best = g.hints == 0 and (st["best"] is None or t < st["best"])
        if new_best:
            st["best"] = t
        if p.label.startswith("daily"):
            self.stats.setdefault("daily", {})[p.label[6:]] = t
        level = getattr(p, "level", None)
        if level:
            n, d, i = level
            pack = self.stats.setdefault("levels", {}).setdefault(level_key(n, d), {})
            old = pack.get(str(i), {}).get("best")
            clean = t if g.hints == 0 else None
            if old is None or (clean is not None and clean < old):
                pack[str(i)] = {"best": clean}  # None = solved, but with hints
        save_stats(self.stats)
        return new_best

    # -- drawing helpers --------------------------------------------------
    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        s = s[: max(0, w - x - (1 if y == h - 1 else 0))]
        if not s:
            return
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def put_segments(self, y, x, segs):
        """Draw [(text, attr), ...] left to right; returns end x."""
        for text, attr in segs:
            self.put(y, x, text, attr)
            x += len(text)
        return x

    fmt_time = staticmethod(fmt_time)

    def draw(self):
        self.scr.erase()
        if self.flash and time.time() > self.flash[3]:
            self.flash = None
        self.hits = []
        if self.boss:
            self.draw_boss()
        elif self.help:
            self.draw_help()
        elif self.screen == "menu":
            self.draw_menu()
        elif self.screen == "levels":
            self.draw_levels()
        elif self.screen == "stats":
            self.draw_stats()
        elif self.stealth:
            self.draw_stealth()
        else:
            self.draw_normal()
        self.scr.refresh()

    def too_small(self, need_h, need_w):
        h, w = self.scr.getmaxyx()
        if h < need_h or w < need_w:
            lines = ["Terminal too small", "need %dx%d, have %dx%d" % (need_w, need_h, w, h),
                     "(enlarge it, or try a smaller -s)"]
            for i, line in enumerate(lines):
                self.put(h // 2 - 1 + i, max(0, (w - len(line)) // 2), line,
                         curses.A_BOLD if i == 0 else self.dim())
            return True
        return False

    def draw_panel(self, title, lines, width):
        """Centered rounded box. lines: list of segment lists."""
        h, w = self.scr.getmaxyx()
        bh = len(lines) + 4
        top = max(0, (h - bh) // 2)
        left = max(0, (w - width) // 2)
        if self.uni:
            tl, tr, bl, br, hz, vt = u"╭", u"╮", u"╰", u"╯", u"─", u"│"
        else:
            tl = tr = bl = br = "+"
            hz, vt = "-", "|"
        line_attr = self.cp(P_LINE)
        self.put(top, left, tl + hz * (width - 2) + tr, line_attr)
        self.put(top, left + 3, " %s " % title, curses.A_BOLD)
        for i in range(bh - 2):
            self.put(top + 1 + i, left, vt, line_attr)
            self.put(top + 1 + i, left + width - 1, vt, line_attr)
        self.put(top + bh - 1, left, bl + hz * (width - 2) + br, line_attr)
        for i, segs in enumerate(lines):
            self.put_segments(top + 2 + i, left + 3, segs)
        return top + 2, left + 3

    def draw_help(self):
        if self.stealth:
            self.put(0, 0, "In [%d]: chk?" % (self.in_no + 2))
            self.put(1, 0, "Signature: chk(blk, tol=3, *, auto=True)")
            self.put(2, 0, "Docstring:")
            for i, line in enumerate(HELP):
                self.put(3 + i, 4, line)
            self.put(4 + len(HELP), 0, "File:      ~/src/blocks/checks.py")
            self.put(5 + len(HELP), 0, "Type:      function")
            return
        bold, dim = curses.A_BOLD, self.dim()
        lines = [[(s, 0)] for s in RULES]
        lines.append([])
        for k, v in HELP_KEYS:
            lines.append([("%-14s" % k, bold | self.cp(P_YELLOW)), (v, 0)])
        lines.append([])
        lines.append([("press any key to return", dim)])
        width = max(len(s) for s in RULES) + 6
        self.draw_panel("meowdokuterm", lines, width)

    def draw_boss(self):
        self.boss.tick()
        h, w = self.scr.getmaxyx()
        lines = self.boss.lines[-(h - 1):]
        for i, line in enumerate(lines):
            self.put(i, 0, line)

    # -- menus --------------------------------------------------------------
    def times(self, n):
        return (u"%d×%d" if self.uni else "%dx%d") % (n, n)

    def lives_text(self, v):
        return str(v) if v > 0 else ("∞" if self.uni else "unlimited")

    def daily_done(self):
        return datetime.date.today().isoformat() in self.stats.get("daily", {})

    def menu_info(self, item):
        """Right-hand text for a main menu row."""
        n, d = self.level_size(), self.level_difficulty()
        if item == "play":
            return "random %s %s" % (self.times(self.size), self.difficulty)
        if item == "levels":
            return "%s %s  %d/%d solved" % (self.times(n), d, len(level_progress(self.stats, n, d)),
                                            LEVEL_COUNT)
        if item == "daily":
            return "solved today " + (u"✓" if self.uni else "(done)") if self.daily_done() \
                else datetime.date.today().isoformat()
        if item == "size":
            return self.times(self.size)
        if item == "difficulty":
            return self.difficulty
        if item == "lives":
            return self.lives_text(self.lives)
        return ""

    MENU_LABELS = dict(play="Play", levels="Levels", daily="Daily puzzle", size="Board size",
                       difficulty="Difficulty", lives="Lives", stats="Stats", help="How to play",
                       quit="Quit")

    def draw_menu(self):
        if self.stealth:
            return self.draw_stealth_menu()
        bold, dim = curses.A_BOLD, self.dim()
        left_a, right_a = (u"◀", u"▶") if self.uni else ("<", ">")
        pointer = u"▸ " if self.uni else "> "
        width = 50
        lines = [[("Put one cat in every row, column and region.", dim)], []]
        rows = {}
        for i, item in enumerate(MENU_ITEMS):
            sel = i == self.menu_sel
            info = self.menu_info(item)
            segs = [(pointer if sel else "  ", bold | self.cp(P_YELLOW)),
                    ("%-14s" % self.MENU_LABELS[item], bold if sel else 0)]
            if item in MENU_SETTINGS:
                segs += [(left_a + " " if sel else "  ", self.cp(P_YELLOW)), (info, bold),
                         (" " + right_a if sel else "", self.cp(P_YELLOW))]
            elif info:
                segs.append((info, dim))
            rows[len(lines)] = i
            lines.append(segs)
            if item in ("daily", "lives"):
                lines.append([])
        lines.append([])
        keys = [(u"↑↓" if self.uni else "up/dn", "choose"), (u"←→" if self.uni else "lt/rt", "change"),
                ("Enter", "select"), ("q", "quit")]
        segs = []
        for k, v in keys:
            segs += [(k, bold | self.cp(P_YELLOW)), (" " + v + "  ", dim)]
        lines.append(segs)
        h, w = self.scr.getmaxyx()
        if self.too_small(len(lines) + 4, width):
            return
        y0, x0 = self.draw_panel("meowdokuterm", lines, width)
        for li, i in rows.items():
            self.hits.append((y0 + li, x0, width - 6, lambda i=i: self.menu_click(i)))

    def draw_stealth_menu(self):
        k = self.in_no
        vals = dict(play="'run'", levels="'%d/%d done'" % (
                        len(level_progress(self.stats, self.level_size(), self.level_difficulty())),
                        LEVEL_COUNT),
                    daily="'%s'" % datetime.date.today().isoformat(), size=str(self.size),
                    difficulty="'%s'" % self.difficulty,
                    lives=str(self.lives) if self.lives > 0 else "None",
                    stats="<dict>", help="<doc>", quit="<exit>")
        self.put(0, 0, "In [%d]: cfg = session.config()" % (k - 1))
        self.put(2, 0, "In [%d]: cfg" % k)
        self.put(3, 0, "Out[%d]:" % k)
        for i, item in enumerate(MENU_ITEMS):
            key = "%s'%s':" % ("{" if i == 0 else " ", item)
            end = "}" if i == len(MENU_ITEMS) - 1 else ","
            text = "%-15s %s%s" % (key, vals[item], end)
            self.put(4 + i, 0, text, curses.A_REVERSE if i == self.menu_sel else 0)
            self.hits.append((4 + i, 0, len(text), lambda i=i: self.menu_click(i)))
        self.put(5 + len(MENU_ITEMS), 0, "In [%d]: " % (k + 1))

    def menu_click(self, i):
        self.menu_sel = i
        self.menu_activate(1)

    def menu_change(self, step):
        item = MENU_ITEMS[self.menu_sel]
        if item == "size":
            self.size = min(MAX_N, max(MIN_N, self.size + step))
        elif item == "difficulty":
            opts = QUICK_DIFFICULTIES
            self.difficulty = opts[(opts.index(self.difficulty) + step) % len(opts)]
        elif item == "lives":
            opts = list(LIVES_CHOICES)
            if self.lives not in opts:
                opts.append(self.lives)
            self.lives = opts[(opts.index(self.lives) + step) % len(opts)]

    def menu_activate(self, step=1):
        """Enter on a menu row. Returns False to quit."""
        item = MENU_ITEMS[self.menu_sel]
        if item in MENU_SETTINGS:
            if item == "size" and self.size == MAX_N and step > 0:
                self.size = MIN_N
            else:
                self.menu_change(step)
        elif item == "play":
            self.start("quick")
        elif item == "levels":
            self.open_levels()
        elif item == "daily":
            self.start("daily")
        elif item == "stats":
            self.screen = "stats"
        elif item == "help":
            self.help = True
        elif item == "quit":
            return False
        return True

    def handle_menu(self, ch):
        if ch in (curses.KEY_UP, ord("k"), ord("w")):
            self.menu_sel = (self.menu_sel - 1) % len(MENU_ITEMS)
        elif ch in (curses.KEY_DOWN, ord("j"), ord("s")):
            self.menu_sel = (self.menu_sel + 1) % len(MENU_ITEMS)
        elif ch in (curses.KEY_LEFT, ord("h"), ord("a")):
            self.menu_change(-1)
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("d")):
            self.menu_change(1)
        elif ch in (10, 13, curses.KEY_ENTER, ord(" "), ord("c")):
            return self.menu_activate()
        elif ch == ord("p"):
            self.start("quick")
        elif ch == ord("L"):
            self.open_levels()
        elif ch in (ord("q"), 27):
            return False
        return True

    def level_cell(self, i):
        """Index (0-based) to the 1-based level number and grid position."""
        return i + 1, divmod(i, LEVEL_COLS)

    def draw_levels(self):
        n, d = self.level_size(), self.level_difficulty()
        done = level_progress(self.stats, n, d)
        nxt = self.first_unsolved(n, d) if len(done) < LEVEL_COUNT else None
        if self.stealth:
            return self.draw_stealth_levels(n, d, done)
        bold, dim = curses.A_BOLD, self.dim()
        left_a, right_a = (u"◀", u"▶") if self.uni else ("<", ">")
        pointer = u"▸ " if self.uni else "> "
        tick = u"✓" if self.uni else "*"
        width = 46
        lines = []
        for f, (label, value) in enumerate((("Board size", self.times(n)), ("Difficulty", d))):
            sel = self.lv_focus == f
            lines.append([(pointer if sel else "  ", bold | self.cp(P_YELLOW)),
                          ("%-14s" % label, bold if sel else 0),
                          (left_a + " " if sel else "  ", self.cp(P_YELLOW)), (value, bold),
                          (" " + right_a if sel else "", self.cp(P_YELLOW))])
        lines.append([])
        grid_top = len(lines)
        rows = (LEVEL_COUNT + LEVEL_COLS - 1) // LEVEL_COLS
        for r in range(rows):
            segs = [("  ", 0)]
            for c in range(LEVEL_COLS):
                i = r * LEVEL_COLS + c
                if i >= LEVEL_COUNT:
                    break
                num = i + 1
                text = " %2d %s " % (num, tick if num in done else " ")
                if self.lv_focus == 2 and i == self.lv_sel:
                    attr = (self.cp(P_CURSOR) | bold) if self.color else curses.A_REVERSE
                elif num in done:
                    attr = self.cp(P_GREEN) | bold
                elif num == nxt:
                    attr = self.cp(P_YELLOW) | bold
                else:
                    attr = dim
                segs += [(text, attr), (" ", 0)]
            lines.append(segs)
        lines.append([])
        num = self.lv_sel + 1
        if num in done:
            info = ("best " + fmt_time(done[num])) if done[num] is not None else "solved with hints"
        else:
            info = "not solved yet"
        lines.append([("Level %d" % num, bold), ("  %s" % info, dim),
                      ("   %d/%d solved" % (len(done), LEVEL_COUNT), self.cp(P_GREEN))])
        if self.msg[0]:
            lines.append([(self.msg[0], self.cp(P_GREEN) | bold)])
        lines.append([])
        segs = []
        for k, v in (("Enter", "play"), (u"←↑↓→" if self.uni else "arrows", "move"), ("Esc", "back")):
            segs += [(k, bold | self.cp(P_YELLOW)), (" " + v + "  ", dim)]
        lines.append(segs)
        if self.too_small(len(lines) + 4, width):
            return
        y0, x0 = self.draw_panel("levels", lines, width)
        for f in (0, 1):
            self.hits.append((y0 + f, x0, width - 6, lambda f=f: self.levels_focus_click(f)))
        for i in range(LEVEL_COUNT):
            r, c = divmod(i, LEVEL_COLS)
            self.hits.append((y0 + grid_top + r, x0 + 2 + 7 * c, 6, lambda i=i: self.levels_click(i)))

    def draw_stealth_levels(self, n, d, done):
        k = self.in_no
        y = 0
        self.put(y, 0, "In [%d]: runs = sweep(" % k)
        x = len("In [%d]: runs = sweep(" % k)
        for f, text in enumerate(("k=%d" % n, ", ", "mode='%s'" % d, ")")):
            focus = {0: 0, 2: 1}.get(f)
            self.put(y, x, text, curses.A_REVERSE if focus is not None and focus == self.lv_focus else 0)
            x += len(text)
        y += 2
        self.put(y, 0, "In [%d]: runs.ids" % (k + 1)); y += 1
        self.put(y, 0, "Out[%d]:" % (k + 1)); y += 1
        rows = (LEVEL_COUNT + LEVEL_COLS - 1) // LEVEL_COLS
        for r in range(rows):
            prefix = "array([[" if r == 0 else "       ["
            self.put(y, 0, prefix)
            x = len(prefix)
            for c in range(LEVEL_COLS):
                i = r * LEVEL_COLS + c
                if i >= LEVEL_COUNT:
                    break
                a = curses.A_BOLD if (i + 1) in done else 0
                if self.lv_focus == 2 and i == self.lv_sel:
                    a |= curses.A_REVERSE
                self.put(y, x, "%2d" % (i + 1), a)
                self.hits.append((y, x, 2, lambda i=i: self.levels_click(i)))
                x += 2
                if c < LEVEL_COLS - 1 and i < LEVEL_COUNT - 1:
                    self.put(y, x, ", ")
                    x += 2
            self.put(y, x, "]," if r < rows - 1 else "]])")
            y += 1
        y += 1
        self.put(y, 0, "# %d/%d converged" % (len(done), LEVEL_COUNT)); y += 2
        self.put(y, 0, "In [%d]: " % (k + 2))

    def levels_focus_click(self, f):
        self.lv_focus = f
        self.levels_change(1)

    def levels_click(self, i):
        self.lv_focus = 2
        self.lv_sel = i
        self.start("level", level=i + 1)

    def levels_change(self, step):
        if self.lv_focus == 0:
            i = LEVEL_SIZES.index(self.level_size())
            self.size = LEVEL_SIZES[(i + step) % len(LEVEL_SIZES)]
        else:
            i = DIFFICULTIES.index(self.level_difficulty())
            self.difficulty = DIFFICULTIES[(i + step) % len(DIFFICULTIES)]
        self.lv_sel = self.first_unsolved(self.level_size(), self.level_difficulty()) - 1

    def handle_levels(self, ch):
        self.say("", "")
        up = ch in (curses.KEY_UP, ord("k"), ord("w"))
        down = ch in (curses.KEY_DOWN, ord("j"), ord("s"))
        left = ch in (curses.KEY_LEFT, ord("h"), ord("a"))
        right = ch in (curses.KEY_RIGHT, ord("l"), ord("d"))
        enter = ch in (10, 13, curses.KEY_ENTER, ord(" "), ord("c"))
        if ch in (27, ord("m"), ord("q"), curses.KEY_BACKSPACE, 127):
            self.open_menu()
        elif self.lv_focus < 2:
            if up:
                self.lv_focus = max(0, self.lv_focus - 1)
            elif down or enter:
                self.lv_focus += 1
            elif left or right:
                self.levels_change(-1 if left else 1)
        else:
            r, c = divmod(self.lv_sel, LEVEL_COLS)
            if up:
                if r == 0:
                    self.lv_focus = 1
                else:
                    self.lv_sel -= LEVEL_COLS
            elif down:
                self.lv_sel = min(LEVEL_COUNT - 1, self.lv_sel + LEVEL_COLS)
            elif left:
                self.lv_sel = (self.lv_sel - 1) % LEVEL_COUNT
            elif right:
                self.lv_sel = (self.lv_sel + 1) % LEVEL_COUNT
            elif enter:
                self.start("level", level=self.lv_sel + 1)
        return True

    def draw_stats(self):
        lines = stats_lines(self.stats) or ["No puzzles solved yet."]
        if self.stealth:
            self.put(0, 0, "In [%d]: session.stats" % self.in_no)
            self.put(1, 0, "Out[%d]:" % self.in_no)
            for i, line in enumerate(lines):
                self.put(2 + i, 0, "  " + line)
            return
        segs = [[(s, 0)] for s in lines] + [[], [("press any key to return", self.dim())]]
        width = max(40, max(len(s) for s in lines) + 6)
        if not self.too_small(len(segs) + 4, width):
            self.draw_panel("stats", segs, width)

    # -- the board ----------------------------------------------------------
    def region_attr(self, reg):
        return self.pair(self.ink, self.rcol[reg]) if self.npal else 0

    def assign_colours(self, p):
        """Give each region a colour, keeping neighbours as different as possible."""
        if not self.npal:
            self.rcol = []
            return
        n, reg = p.n, p.regions
        adj = [set() for _ in range(n)]
        for r in range(n):
            for c in range(n):
                for rr, cc in ((r + 1, c), (r, c + 1)):
                    if rr < n and cc < n and reg[rr][cc] != reg[r][c]:
                        adj[reg[r][c]].add(reg[rr][cc])
                        adj[reg[rr][cc]].add(reg[r][c])
        rgb = [xterm_rgb(i) for i in self.pal]
        dist = lambda a, b: sum((x - y) ** 2 for x, y in zip(rgb[a], rgb[b]))
        chosen = {}
        for k in sorted(range(n), key=lambda k: (-len(adj[k]), k)):
            near = [chosen[j] for j in adj[k] if j in chosen]
            best = None
            for ci in range(min(len(self.pal), max(n, 8))):
                if ci in near or (len(self.pal) >= n and ci in chosen.values()):
                    continue
                score = min([dist(ci, j) for j in near] or [10 ** 9])
                if best is None or score > best[0]:
                    best = (score, ci)
            chosen[k] = best[1] if best else 0
        self.rcol = [self.pal[chosen[k]] for k in range(n)]

    def cell_look(self, r, c, use_letters):
        """(text, attr) for a cell's 3-column body."""
        g, p = self.game, self.game.p
        v = g.grid[r][c]
        reg = p.regions[r][c]
        attr = self.region_attr(reg)
        reveal = g.state == "lost" and p.solution[r] == c and v != CAT
        if v == CAT or reveal:
            text = self.gl["cat"]
            attr |= curses.A_BOLD
        elif v == MARK and g.state != "won":
            text = self.gl["mark"]
        elif g.state == "won":
            text = "   "
        else:
            text = (" %s " % LETTERS[reg]) if use_letters else self.gl["empty"]
        if not self.color and v == EMPTY and not use_letters:
            text = "   "
        if self.flash and (self.flash[0], self.flash[1]) == (r, c):
            attr = self.cp(P_FLASH_BAD if self.flash[2] == "bad" else P_FLASH_GOOD) | curses.A_BOLD
        elif [r, c] == g.cur and g.state == "play":
            attr = (self.cp(P_CURSOR) | curses.A_BOLD) if self.color else curses.A_REVERSE
            if v == EMPTY and not use_letters:
                text = u" • " if self.uni else " o "
        if reveal:
            attr |= curses.A_UNDERLINE
        return text, attr

    def board_size(self, n, borders):
        return ((2 * n + 1, 4 * n + 1) if borders else (n + 2, 3 * n + 2))

    def draw_tiles(self, top, left, hseg, vseg, use_letters):
        """Regions as solid colour tiles split by thin dark lines.

        Every cell sits on a 4x2 character pitch. The row under each cell row is
        drawn with half blocks so a region border there is only half a row thick,
        about as thick as the one-column vertical borders.
        """
        n, reg = self.game.p.n, self.game.p.regions

        def rid(r, c):
            return reg[r][c] if 0 <= r < n and 0 <= c < n else None

        def colour(k):
            return None if k is None else self.rcol[k]

        def half(top_c, bot_c):
            if top_c == bot_c:
                return " ", self.pair(self.ink, top_c)
            if not self.uni:  # no half blocks: the whole row becomes the line
                return " ", self.pair(None, None)
            if bot_c is None:
                return u"▀", self.pair(top_c, None)
            return u"▄", self.pair(bot_c, top_c)

        for r in range(n + 1):
            y = top + 2 * r
            for c in range(n + 1):
                x = left + 4 * c
                tl, tr, bl, br = rid(r - 1, c - 1), rid(r - 1, c), rid(r, c - 1), rid(r, c)
                crossed = (c > 0 and hseg(r, c - 1)) or (c < n and hseg(r, c))
                t = colour(tl) if (tl is not None and tl == tr and not crossed) else None
                b = colour(bl) if (bl is not None and bl == br) else None
                self.put(y, x, *half(t, b))
                if c < n:
                    t = None if hseg(r, c) else colour(tr)
                    ch, attr = half(t, colour(br))
                    self.put(y, x + 1, ch * 3, attr)
            if r == n:
                break
            for c in range(n + 1):
                x = left + 4 * c
                if vseg(r, c):
                    self.put(y + 1, x, " ", self.pair(None, None))
                else:
                    self.put(y + 1, x, " ", self.pair(self.ink, colour(reg[r][c])))
                if c < n:
                    text, attr = self.cell_look(r, c, use_letters)
                    self.put(y + 1, x + 1, text, attr)
                    self.cellpos[(r, c)] = (y + 1, x, 4)

    def draw_board(self, top, left, borders, use_letters):
        p = self.game.p
        n, reg = p.n, p.regions
        line = self.cp(P_LINE)
        self.cellpos = {}
        box = (lambda k: ROUND.get(k) or BOX[k]) if self.uni else ascii_box
        hz = u"─" if self.uni else "-"
        vt = u"│" if self.uni else "|"
        if not borders:
            self.put(top, left, box((0, 1, 0, 1)) + hz * (3 * n) + box((0, 1, 1, 0)), line)
            for r in range(n):
                y = top + 1 + r
                self.put(y, left, vt, line)
                for c in range(n):
                    x = left + 1 + 3 * c
                    text, attr = self.cell_look(r, c, use_letters)
                    self.put(y, x, text, attr)
                    self.cellpos[(r, c)] = (y, x, 3)
                self.put(y, left + 1 + 3 * n, vt, line)
            self.put(top + n + 1, left, box((1, 0, 0, 1)) + hz * (3 * n) + box((1, 0, 1, 0)), line)
            return

        def hseg(r, c):  # boundary above row r, column c
            return r == 0 or r == n or reg[r - 1][c] != reg[r][c]

        def vseg(r, c):  # boundary left of column c, row r
            return c == 0 or c == n or reg[r][c - 1] != reg[r][c]

        if self.color:
            self.draw_tiles(top, left, hseg, vseg, use_letters)
            return

        for r in range(n + 1):
            y = top + 2 * r
            for c in range(n + 1):
                x = left + 4 * c
                key = (int(r > 0 and vseg(r - 1, c)), int(r < n and vseg(r, c)),
                       int(c > 0 and hseg(r, c - 1)), int(c < n and hseg(r, c)))
                if any(key):
                    self.put(y, x, box(key), line)
                else:
                    self.put(y, x, " ", self.region_attr(reg[r][c]))
                if c < n:
                    if hseg(r, c):
                        self.put(y, x + 1, hz * 3, line)
                    else:
                        self.put(y, x + 1, "   ", self.region_attr(reg[r][c]))
            if r == n:
                break
            for c in range(n + 1):
                x = left + 4 * c
                if vseg(r, c):
                    self.put(y + 1, x, vt, line)
                else:
                    self.put(y + 1, x, " ", self.region_attr(reg[r][c]))
                if c < n:
                    text, attr = self.cell_look(r, c, use_letters)
                    self.put(y + 1, x + 1, text, attr)
                    self.cellpos[(r, c)] = (y + 1, x, 4)

    def draw_normal(self):
        g, p = self.game, self.game.p
        n = p.n
        gl = self.gl
        h, w = self.scr.getmaxyx()
        borders = self.borders or not self.color
        bh, bw = self.board_size(n, borders)
        if borders and self.color and h < bh + 6:  # fall back to the compact board
            borders = False
            bh, bw = self.board_size(n, borders)
        # without borders, regions are only told apart by colour
        use_letters = self.letters or (not borders and n > self.npal)
        box_w = max(bw, 46)
        if self.too_small(bh + 6, box_w + 2):
            return
        top = max(0, (h - (bh + 6)) // 2)
        left = max(0, (w - box_w) // 2)
        bold, dim = curses.A_BOLD, self.dim()

        # header: title + info on the left, lives + timer on the right
        name = " meowdokuterm " if self.rich else "meowdokuterm"
        info = [("  %d×%d" % (n, n) if self.uni else "  %dx%d" % (n, n), 0),
                (gl["sep"] + p.difficulty, dim)]
        if p.label:
            info.append((gl["sep"] + p.label, dim))
        elif p.seed is not None:
            info.append((gl["sep"] + "#%s" % p.seed, dim))
        if g.max_lives > 0:
            hearts = " ".join([gl["heart"]] * g.lives + [gl["heart_off"]] * (g.max_lives - g.lives))
        else:
            hearts = ""
        clock = self.fmt_time(g.elapsed())
        right = len(hearts) + 3 + len(clock)
        # rather than run into the lives/clock, drop the seed, or the difficulty before a label
        while len(info) > 1 and len(name) + sum(len(t) for t, _ in info) + right + 1 > box_w:
            info.pop(1 if p.label else -1)
        self.put_segments(top, left, [(name, (self.cp(P_TITLE) | bold) if self.rich else bold)] + info)
        self.put_segments(top, left + box_w - right,
                          [(hearts, self.cp(P_RED) | bold), ("   ", 0), (clock, bold)])

        # board, centred under the header
        by = top + 2
        bx = left + (box_w - bw) // 2
        self.draw_board(by, bx, borders, use_letters)

        # progress
        y = by + bh + 1
        cats = g.cats()
        bar = gl["on"] * cats + gl["off"] * (n - cats)
        segs = [(bar, self.cp(P_GREEN) | bold if cats else dim), ("  %d/%d cats" % (cats, n), dim)]
        if g.hints:
            segs.append((gl["sep"] + "%d hint%s" % (g.hints, "s" if g.hints > 1 else ""), dim))
        if not self.automark:
            segs.append((gl["sep"] + "auto-mark off", dim))
        width = sum(len(s) for s, _ in segs)
        self.put_segments(y, max(0, (w - width) // 2), segs)

        # message
        msg = self.msg[0]
        kind = self.msg_kind
        if g.state == "won":
            kind = "good"
        elif g.state == "lost":
            kind = "bad"
        attr = {"good": self.cp(P_GREEN) | bold, "bad": self.cp(P_RED) | bold}.get(kind, 0)
        if msg:
            self.put(y + 1, max(0, (w - len(msg)) // 2), msg, attr)

        # key hints
        if g.state == "play":
            # least important last: they are dropped first on narrow terminals
            keys = [("c", "cat"), ("x", "mark"), ("?", "help"), ("m", "menu"), ("b", "boss"),
                    ("Tab", "stealth"), ("u", "undo"), ("H", "hint")]
        else:
            nxt = "next level" if self.mode == "level" else "next puzzle"
            keys = [("n", nxt), ("r", "retry"), ("m", "menu"), ("q", "quit"), ("Tab", "stealth")]
        while True:
            segs = []
            for i, (k, v) in enumerate(keys):
                if i:
                    segs.append(("  ", 0))
                segs += [(k, bold | self.cp(P_YELLOW)), (" " + v, dim)]
            width = sum(len(s) for s, _ in segs)
            if width <= w - 2 or len(keys) <= 3:
                break
            keys = keys[:-1]
        self.put_segments(y + 3, max(0, (w - width) // 2), segs)

    def draw_stealth(self):
        g, p = self.game, self.game.p
        n = p.n
        k = self.in_no
        need_w = 9 + 4 * n + 4
        if self.too_small(n + 14, max(need_w, 50)):
            return
        y = 0
        fname = "cov_%03d.npz" % ((p.seed or 0) % 1000 if isinstance(p.seed, int) else n)
        self.put(y, 0, "In [%d]: blk = load_block(run_dir / %r, k=%d)" % (k - 1, fname, n)); y += 2
        self.put(y, 0, "In [%d]: blk" % k); y += 1
        self.put(y, 0, "Out[%d]:" % k); y += 1
        self.put(y, 0, "masked_array("); y += 1
        self.cellpos = {}
        lost_sol = set((r, c) for r, c in enumerate(p.solution)) if g.state == "lost" else set()
        for r in range(n):
            prefix = "  data=[[" if r == 0 else "        ["
            self.put(y, 0, prefix)
            x = len(prefix)
            for c in range(n):
                v = g.grid[r][c]
                if v == CAT or (r, c) in lost_sol:
                    s, a = "-1", curses.A_BOLD
                elif v == MARK:
                    s, a = "--", 0
                else:
                    s, a = "%2d" % p.regions[r][c], 0
                if [r, c] == g.cur and g.state == "play":
                    a |= curses.A_REVERSE
                self.put(y, x, s, a)
                self.cellpos[(r, c)] = (y, x, 4)
                x += 2
                if c < n - 1:
                    self.put(y, x, ", ")
                    x += 2
            self.put(y, x, "]," if r < n - 1 else "]],")
            y += 1
        self.put(y, 0, "  fill_value=999999)"); y += 2
        tol = g.lives if g.max_lives > 0 else "None"
        self.put(y, 0, "In [%d]: chk(blk, tol=%s, auto=%s)" % (k + 1, tol, self.automark)); y += 1
        if g.state == "won":
            self.put(y, 0, "Out[%d]: True" % (k + 1)); y += 1
            self.put(y, 0, "# converged: %d it, %.1f s wall" % (g.mistakes * 7 + n * 31 + g.hints, g.elapsed())); y += 1
        elif self.msg[1]:
            for line in self.msg[1].split("\n"):
                self.put(y, 0, line); y += 1
        y += 1
        self.put(y, 0, "In [%d]: " % (k + 2))
        if self.confirm:
            self.put(y + 1, 0, "Do you really want to %s ([y]/n)? "
                     % ("exit" if self.confirm == "quit" else "restart the kernel"))

    # -- input ------------------------------------------------------------
    def move(self, dr, dc):
        n = self.game.p.n
        self.game.cur[0] = (self.game.cur[0] + dr) % n
        self.game.cur[1] = (self.game.cur[1] + dc) % n

    def do_cat(self, r, c):
        g = self.game
        res = g.place_cat(r, c, self.automark)
        if res in ("wrong", "lost"):
            self.flash = (r, c, "bad", time.time() + 0.7)
        if res == "wrong":
            if g.max_lives > 0:
                self.say("Hiss! No cat belongs there. %d %s left." % (g.lives, "life" if g.lives == 1 else "lives"),
                         "/home/%s/src/blocks/checks.py:212: RuntimeWarning: invalid value encountered in chk\n"
                         "  resid = np.divide(a, b)  # tol now %d" % (self.boss_user(), g.lives), kind="bad")
            else:
                self.say("Hiss! No cat belongs there.",
                         "RuntimeWarning: invalid value encountered in chk", kind="bad")
        elif res == "lost":
            self.say("Out of lives! The answer is shown underlined.",
                     "---------------------------------------------------------------------------\n"
                     "ConvergenceError: tol exhausted (r: rerun, n: next block)", kind="bad")
        elif res == "won":
            best = self.record_win()
            level = getattr(g.p, "level", None)
            what = ("Level %d done" % level[2]) if level else "Solved"
            self.say("Purrfect! %s in %s%s" %
                     (what, self.fmt_time(g.elapsed()), " - a new best!" if best else "."), "", kind="good")
        elif res == "ok":
            self.say("", "")

    def boss_user(self):
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "user"

    def handle_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return True
        if self.screen != "game":
            if bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                for y, x, w, action in self.hits:
                    if my == y and x <= mx < x + w:
                        return action() is not False
            return True
        for (r, c), (y, x, w) in self.cellpos.items():
            if my == y and x <= mx < x + w:
                self.game.cur = [r, c]
                if bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                    self.do_cat(r, c)
                elif bstate & (curses.BUTTON3_PRESSED | curses.BUTTON3_CLICKED):
                    self.game.toggle_mark(r, c)
                return True
        return True

    def handle(self, ch):
        g = self.game
        if self.boss:
            if ch == ord("b"):
                self.boss = None
                if g:
                    g.resume()
            return True
        if self.help:
            self.help = False
            if g:
                g.resume()
            return True
        # keys that work on every screen
        if ch == 9:
            self.stealth = not self.stealth
            return True
        if ch == ord("b"):
            if g:
                g.pause()
            self.boss = FakeLog()
            return True
        if ch == ord("?"):
            if g:
                g.pause()
            self.help = True
            return True
        if ch == curses.KEY_MOUSE:
            return self.handle_mouse()
        if self.screen == "menu":
            return self.handle_menu(ch)
        if self.screen == "levels":
            return self.handle_levels(ch)
        if self.screen == "stats":
            self.screen = "menu"
            return True
        if self.confirm:
            if ch in (ord("y"), ord("Y"), 10, 13):
                if self.confirm == "quit":
                    return False
                self.leave_game()
                return True
            self.confirm = None
            self.say("", "")
            return True
        r, c = g.cur
        if ch in (curses.KEY_UP, ord("k"), ord("w")):
            self.move(-1, 0)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.move(1, 0)
        elif ch in (curses.KEY_LEFT, ord("h")):
            self.move(0, -1)
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.move(0, 1)
        elif ch in (ord("c"), 10, 13, curses.KEY_ENTER):
            self.do_cat(r, c)
        elif ch in (ord("x"), ord(".")):
            g.toggle_mark(r, c)
        elif ch == ord(" "):
            if g.grid[r][c] == EMPTY:
                g.toggle_mark(r, c)
            elif g.grid[r][c] == MARK:
                self.do_cat(r, c)
        elif ch == ord("u"):
            if not g.undo():
                self.say("Nothing to undo.", "")
        elif ch == ord("C"):
            if g.state == "play":
                g.snapshot()
                g.grid = [[CAT if v == CAT else EMPTY for v in row] for row in g.grid]
        elif ch == ord("H"):
            res = g.hint(self.automark)
            if res:
                (hr, hc), outcome = res
                if outcome == "won":
                    self.record_win()
                    self.say("Solved in %s (with %d hint%s)." % (self.fmt_time(g.elapsed()), g.hints,
                                                                  "s" if g.hints > 1 else ""), "", kind="good")
                else:
                    self.flash = (hr, hc, "good", time.time() + 0.9)
                    self.say("Hint: there's a cat at row %d, column %d." % (hr + 1, hc + 1),
                             "Out[%d]: (%d, %d)" % (self.in_no + 1, hr, hc))
        elif ch == ord("a"):
            self.automark = not self.automark
            self.say("Auto-mark %s." % ("on" if self.automark else "off"), "")
        elif ch == ord("L"):
            self.letters = not self.letters
        elif ch == ord("B"):
            self.borders = not self.borders
        elif ch == ord("r"):
            self.new_game(g.p)
        elif ch == ord("n"):
            self.next_puzzle()
        elif ch in (ord("m"), 27):
            if g.state == "play" and g.cats() > 0:
                self.confirm = "menu"
                self.say("Leave this puzzle for the menu?  y / n", "", kind="bad")
            else:
                self.leave_game()
        elif ch == ord("q"):
            if g.state == "play" and g.cats() > 0:
                self.confirm = "quit"
                self.say("Quit and lose this puzzle?  y / n", "", kind="bad")
            else:
                return False
        return True

    def run(self):
        while True:
            self.draw()
            ch = self.scr.getch()
            if ch == -1 or ch == curses.KEY_RESIZE:
                continue
            if not self.handle(ch):
                break


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def size_arg(s):
    v = int(s)
    if not MIN_N <= v <= MAX_N:
        raise argparse.ArgumentTypeError("size must be between %d and %d" % (MIN_N, MAX_N))
    return v


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="meshcheck",
        description="Cat-placement logic puzzle for the terminal. One cat per row, column "
                    "and region; cats may not touch, even diagonally.")
    ap.add_argument("-s", "--size", type=size_arg, default=8, help="board size N (4-14, default 8)")
    ap.add_argument("-d", "--difficulty", choices=DIFFICULTIES + ("any",), default="medium",
                    help="difficulty (default medium)")
    ap.add_argument("--seed", type=int, help="seed for a reproducible puzzle")
    ap.add_argument("--daily", action="store_true", help="play today's daily puzzle (8x8 medium)")
    ap.add_argument("--level", type=int, metavar="N",
                    help="play level N (1-%d) of the -s/-d level pack, skipping the menu" % LEVEL_COUNT)
    ap.add_argument("--play", action="store_true", help="skip the menu and start a random puzzle")
    ap.add_argument("--lives", type=int, default=3, help="lives per puzzle, 0 = unlimited (default 3)")
    ap.add_argument("--stealth", action="store_true", help="start in stealth mode (Tab toggles)")
    ap.add_argument("--no-color", action="store_true", help="monochrome; uses borders and letters")
    ap.add_argument("--letters", action="store_true", help="show region letters")
    ap.add_argument("--compact", action="store_true", help="colour-only board without region borders (B toggles)")
    ap.add_argument("--ascii", action="store_true", help="plain ASCII drawing (for terminals without UTF-8)")
    ap.add_argument("--cat", choices=("emoji", "face", "ascii"), default="emoji",
                    help="how cats are drawn: emoji (default), face (=w=), or ascii (=^=)")
    ap.add_argument("--no-automark", action="store_true", help="don't auto-mark cells ruled out by a cat")
    ap.add_argument("--print", dest="print_n", type=int, metavar="N",
                    help="generate N puzzles, print them as text and exit")
    ap.add_argument("--solutions", action="store_true", help="with --print, include solutions")
    ap.add_argument("--load", metavar="FILE", help="play puzzles from a text file (see --print)")
    ap.add_argument("--index", type=int, help="with --load, start at puzzle number INDEX (1-based)")
    ap.add_argument("--stats", action="store_true", help="show your solve stats and exit")
    ap.add_argument("--version", action="version", version="meshcheck " + VERSION)
    args = ap.parse_args(argv)
    if args.level is not None:
        if not 1 <= args.level <= LEVEL_COUNT:
            ap.error("--level must be between 1 and %d" % LEVEL_COUNT)
        if args.size not in LEVEL_SIZES or args.difficulty not in DIFFICULTIES:
            ap.error("levels exist for sizes %d-%d and difficulties %s"
                     % (LEVEL_SIZES[0], LEVEL_SIZES[-1], "/".join(DIFFICULTIES)))

    if args.stats:
        st = load_stats()
        if not st:
            print("No puzzles solved yet. Stats live in " + stats_path())
        for line in stats_lines(st):
            print(line)
        return 0

    if args.print_n:
        seed0 = args.seed if args.seed is not None else random.randrange(10 ** 6)
        for i in range(args.print_n):
            p = generate(args.size, args.difficulty, seed0 + i)
            if i:
                print()
            print(puzzle_to_text(p, args.solutions))
            sys.stdout.flush()
        return 0

    if curses is None:
        print("This Python has no curses module, so meshcheck can't draw the board.\n"
              "Try another python3 (e.g. /usr/bin/python3, `module load python`, or conda).\n"
              "(--print still works without it.)", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    file_source = FileSource(args.load, args.index) if args.load else None
    if not sys.stdout.isatty():
        print("meshcheck needs an interactive terminal (use --print to generate puzzles).", file=sys.stderr)
        return 1

    def _run(scr):
        App(scr, args, file_source).run()

    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
