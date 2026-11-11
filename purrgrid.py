#!/usr/bin/env python3
"""
purrgrid - a cat-placement logic puzzle for the terminal.

Rules: place exactly one cat in every row, every column and every coloured
region. Cats are territorial: no two cats may touch, not even diagonally.
Every generated puzzle has exactly one solution. A wrong cat costs a life.

Pure Python 3.6+ standard library (curses). No installs needed.
Run `purrgrid.py --help` for options, press `?` in game for keys.
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
    lines = ["# purrgrid size=%d difficulty=%s seed=%s" % (p.n, p.difficulty, p.seed)]
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

def stats_path():
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "purrgrid", "stats.json")


def load_stats():
    try:
        with open(stats_path()) as f:
            return json.load(f)
    except Exception:
        return {}


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

class Source(object):
    def __init__(self, args):
        self.args = args
        self.file_puzzles = None
        self.idx = -1
        self.first = True
        if args.load:
            with open(args.load) as f:
                self.file_puzzles = parse_puzzles(f.read())
            if not self.file_puzzles:
                raise SystemExit("no puzzles found in " + args.load)
            self.idx = (args.index - 1) - 1 if args.index else -1

    def next(self):
        a = self.args
        if self.file_puzzles:
            self.idx = (self.idx + 1) % len(self.file_puzzles)
            p = self.file_puzzles[self.idx]
            p.label = "%s #%d/%d" % (os.path.basename(a.load), self.idx + 1, len(self.file_puzzles))
            return p
        if self.first and a.daily:
            self.first = False
            return daily_puzzle()
        seed = a.seed if (self.first and a.seed is not None) else None
        self.first = False
        return generate(a.size, a.difficulty, seed)


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
    ("n  r  q", "new puzzle  /  restart  /  quit"),
]

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
    def __init__(self, scr, args, source):
        self.scr = scr
        self.args = args
        self.source = source
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
        self.new_game(first=True)

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
    def new_game(self, first=False, puzzle=None):
        if puzzle is None:
            self.status_only("Generating a %dx%d %s puzzle..." % (self.args.size, self.args.size,
                                                                 self.args.difficulty), "Running cells...")
            puzzle = self.source.next()
        self.game = Game(puzzle, self.args.lives)
        self.assign_colours(puzzle)
        self.flash = None
        self.in_no += 3
        if getattr(puzzle, "ambiguous", False):
            self.say("Warning: this puzzle has more than one solution.",
                     "UserWarning: system is under-determined", kind="bad")
        else:
            self.say("" if not first else "New here? Press ? for the rules and keys.", "")

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

    @staticmethod
    def fmt_time(t):
        t = int(t)
        return "%d:%02d:%02d" % (t // 3600, t // 60 % 60, t % 60) if t >= 3600 else "%02d:%02d" % (t // 60, t % 60)

    def draw(self):
        self.scr.erase()
        if self.flash and time.time() > self.flash[3]:
            self.flash = None
        if self.boss:
            self.draw_boss()
        elif self.help:
            self.draw_help()
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
        self.draw_panel("purrgrid", lines, width)

    def draw_boss(self):
        self.boss.tick()
        h, w = self.scr.getmaxyx()
        lines = self.boss.lines[-(h - 1):]
        for i, line in enumerate(lines):
            self.put(i, 0, line)

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
        name = " purrgrid " if self.rich else "purrgrid"
        info = [("  %d×%d" % (n, n) if self.uni else "  %dx%d" % (n, n), 0),
                (gl["sep"] + p.difficulty, dim)]
        if p.label:
            info.append((gl["sep"] + p.label, dim))
        elif p.seed is not None:
            info.append((gl["sep"] + "#%s" % p.seed, dim))
        self.put_segments(top, left, [(name, (self.cp(P_TITLE) | bold) if self.rich else bold)] + info)
        if g.max_lives > 0:
            hearts = " ".join([gl["heart"]] * g.lives + [gl["heart_off"]] * (g.max_lives - g.lives))
        else:
            hearts = ""
        clock = self.fmt_time(g.elapsed())
        right = len(hearts) + 3 + len(clock)
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
            keys = [("c", "cat"), ("x", "mark"), ("?", "help"), ("b", "boss"), ("Tab", "stealth"),
                    ("u", "undo"), ("H", "hint")]
        else:
            keys = [("n", "next puzzle"), ("r", "retry"), ("Tab", "stealth"), ("q", "quit")]
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
            self.put(y + 1, 0, "Do you really want to exit ([y]/n)? ")

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
            self.say("Purrfect! Solved in %s%s" %
                     (self.fmt_time(g.elapsed()), " - a new best!" if best else "."), "", kind="good")
        elif res == "ok":
            self.say("", "")

    def boss_user(self):
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "user"

    def handle_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        for (r, c), (y, x, w) in self.cellpos.items():
            if my == y and x <= mx < x + w:
                self.game.cur = [r, c]
                if bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                    self.do_cat(r, c)
                elif bstate & (curses.BUTTON3_PRESSED | curses.BUTTON3_CLICKED):
                    self.game.toggle_mark(r, c)
                return

    def handle(self, ch):
        g = self.game
        if self.boss:
            if ch == ord("b"):
                self.boss = None
                g.resume()
            return True
        if self.help:
            self.help = False
            g.resume()
            return True
        if self.confirm:
            if ch in (ord("y"), ord("Y"), 10, 13):
                return False
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
        elif ch == 9:
            self.stealth = not self.stealth
        elif ch == ord("b"):
            g.pause()
            self.boss = FakeLog()
        elif ch == ord("?"):
            g.pause()
            self.help = True
        elif ch == ord("r"):
            self.new_game(puzzle=g.p)
        elif ch == ord("n"):
            self.new_game()
        elif ch in (ord("q"), 27):
            if g.state == "play" and g.cats() > 0:
                self.confirm = "quit"
                self.say("Quit and lose this puzzle?  y / n", "", kind="bad")
            else:
                return False
        elif ch == curses.KEY_MOUSE:
            self.handle_mouse()
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
        prog="purrgrid",
        description="Cat-placement logic puzzle for the terminal. One cat per row, column "
                    "and region; cats may not touch, even diagonally.")
    ap.add_argument("-s", "--size", type=size_arg, default=8, help="board size N (4-14, default 8)")
    ap.add_argument("-d", "--difficulty", choices=DIFFICULTIES + ("any",), default="medium",
                    help="difficulty (default medium)")
    ap.add_argument("--seed", type=int, help="seed for a reproducible puzzle")
    ap.add_argument("--daily", action="store_true", help="play today's daily puzzle (8x8 medium)")
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
    ap.add_argument("--version", action="version", version="purrgrid " + VERSION)
    args = ap.parse_args(argv)

    if args.stats:
        st = load_stats()
        if not st:
            print("No puzzles solved yet. Stats live in " + stats_path())
        for k in sorted(st):
            if k == "daily":
                print("daily puzzles solved: %d" % len(st[k]))
            else:
                best = st[k]["best"]
                print("%-14s solved %4d   best %s" % (k, st[k]["solved"],
                                                      App.fmt_time(best) if best is not None else "-"))
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
        print("This Python has no curses module, so purrgrid can't draw the board.\n"
              "Try another python3 (e.g. /usr/bin/python3, `module load python`, or conda).\n"
              "(--print still works without it.)", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    source = Source(args)
    if not sys.stdout.isatty():
        print("purrgrid needs an interactive terminal (use --print to generate puzzles).", file=sys.stderr)
        return 1

    def _run(scr):
        App(scr, args, source).run()

    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
