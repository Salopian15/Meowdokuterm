"""Tests for meshcheck (meowdokuterm). Run with:  python3 -m unittest discover -s tests -v"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import meshcheck as M  # noqa: E402


def check_solution(test, p):
    """One cat per row, column and region, and no two cats touching."""
    n = p.n
    cells = [(r, c) for r, c in enumerate(p.solution)]
    test.assertEqual(sorted(c for _, c in cells), list(range(n)))
    test.assertEqual(sorted(p.regions[r][c] for r, c in cells), list(range(n)))
    for (r1, c1), (r2, c2) in zip(cells, cells[1:]):
        test.assertGreater(abs(c1 - c2), 1, "cats in rows %d and %d touch" % (r1, r2))


def canonical(regions):
    """Region ids renumbered in reading order, so equal layouts compare equal."""
    ids = {}
    return [[ids.setdefault(k, len(ids)) for k in row] for row in regions]


def regions_connected(p):
    n = p.n
    for k in range(n):
        cells = set((r, c) for r in range(n) for c in range(n) if p.regions[r][c] == k)
        start = next(iter(cells))
        seen, stack = {start}, [start]
        while stack:
            r, c = stack.pop()
            for dr, dc in M.NEIGH4:
                q = (r + dr, c + dc)
                if q in cells and q not in seen:
                    seen.add(q)
                    stack.append(q)
        if seen != cells:
            return False
    return True


class TempDataDir(unittest.TestCase):
    """Point XDG_DATA_HOME at a temp dir so tests never touch real stats."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = self.tmp

    def tearDown(self):
        if self.old_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self.old_xdg
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestGenerator(unittest.TestCase):
    def test_puzzles_are_valid_and_unique(self):
        for n in (5, 6, 7, 8):
            for d in M.DIFFICULTIES:
                for seed in (1, 2):
                    p = M.generate(n, d, seed)
                    with self.subTest(n=n, difficulty=d, seed=seed):
                        check_solution(self, p)
                        self.assertTrue(regions_connected(p))
                        sols = M.solve_all(M.Geometry(n, p.regions), 2)
                        self.assertEqual(len(sols), 1)
                        self.assertEqual(sols[0], sorted(r * n + c for r, c in enumerate(p.solution)))
                        self.assertEqual(M.rate(M.Geometry(n, p.regions)), p.difficulty)

    def test_same_seed_same_puzzle(self):
        a = M.generate(8, "medium", 1234)
        b = M.generate(8, "medium", 1234)
        self.assertEqual(a.regions, b.regions)

    def test_daily_is_8x8_medium(self):
        p = M.daily_puzzle()
        self.assertEqual(p.n, 8)
        self.assertTrue(p.label.startswith("daily "))


class TestLevels(TempDataDir):
    def test_levels_are_deterministic_and_cached(self):
        a = M.level_puzzle(7, "hard", 3)
        self.assertTrue(os.path.exists(M.level_cache_path()))
        os.remove(M.level_cache_path())
        b = M.level_puzzle(7, "hard", 3)       # regenerated from scratch
        c = M.level_puzzle(7, "hard", 3)       # from the cache
        for p in (b, c):
            self.assertEqual(p.regions, a.regions)
            self.assertEqual(p.solution, a.solution)
        self.assertEqual(c.level, (7, "hard", 3))
        self.assertEqual(c.label, "level 3")

    def test_levels_differ(self):
        self.assertNotEqual(M.level_puzzle(6, "easy", 1).regions, M.level_puzzle(6, "easy", 2).regions)

    def test_stale_cache_is_ignored(self):
        M.write_json(M.level_cache_path(), {"version": -1, "6x6-easy": {"1": {"bad": True}}})
        self.assertIsNone(M.cached_level(6, "easy", 1))
        p = M.level_puzzle(6, "easy", 1)
        check_solution(self, p)

    def test_corrupt_cache_is_ignored(self):
        os.makedirs(os.path.dirname(M.level_cache_path()))
        with open(M.level_cache_path(), "w") as f:
            f.write("{not json")
        check_solution(self, M.level_puzzle(6, "easy", 1))


class TestTextFormat(unittest.TestCase):
    def test_round_trip(self):
        p = M.generate(7, "medium", 99)
        (q,) = M.parse_puzzles(M.puzzle_to_text(p, with_solution=True))
        self.assertEqual(canonical(q.regions), canonical(p.regions))  # ids get renumbered
        self.assertEqual(q.solution, p.solution)
        self.assertEqual(q.difficulty, p.difficulty)
        self.assertFalse(q.ambiguous)

    def test_multiple_puzzles(self):
        text = "\n\n".join(M.puzzle_to_text(M.generate(6, "easy", s)) for s in (1, 2, 3))
        self.assertEqual(len(M.parse_puzzles(text)), 3)

    def test_bad_puzzles(self):
        with self.assertRaises(ValueError):
            M.parse_puzzles("abc\nab\nabc")             # not square
        with self.assertRaises(ValueError):
            M.parse_puzzles("aaaa\naaaa\nbbbb\nbbbb")   # 2 regions on a 4x4


class TestGame(unittest.TestCase):
    def setUp(self):
        self.p = M.generate(6, "easy", 5)
        self.wrong = [(r, (c + 2) % 6) for r, c in enumerate(self.p.solution)]

    def test_win(self):
        g = M.Game(self.p, 3)
        results = [g.place_cat(r, c, True) for r, c in enumerate(self.p.solution)]
        self.assertEqual(results[-1], "won")
        self.assertEqual(g.state, "won")

    def test_wrong_cats_cost_lives(self):
        g = M.Game(self.p, 2)
        self.assertEqual(g.place_cat(*self.wrong[0], automark=True), "wrong")
        self.assertEqual(g.lives, 1)
        self.assertEqual(g.place_cat(*self.wrong[1], automark=True), "lost")
        self.assertEqual(g.state, "lost")

    def test_unlimited_lives(self):
        g = M.Game(self.p, 0)
        for r, c in self.wrong:
            self.assertEqual(g.place_cat(r, c, True), "wrong")
        self.assertEqual(g.state, "play")

    def test_hints_solve_the_puzzle(self):
        g = M.Game(self.p, 3)
        outcome = None
        for _ in range(6):
            _, outcome = g.hint(True)
        self.assertEqual(outcome, "won")
        self.assertEqual(g.hints, 6)

    def test_undo(self):
        g = M.Game(self.p, 3)
        g.toggle_mark(0, 0)
        self.assertEqual(g.grid[0][0], M.MARK)
        self.assertTrue(g.undo())
        self.assertEqual(g.grid[0][0], M.EMPTY)


class TestSavedData(TempDataDir):
    def test_session_round_trip(self):
        p = M.level_puzzle(6, "medium", 2)
        g = M.Game(p, 3)
        g.place_cat(0, p.solution[0], True)
        g.place_cat(1, (p.solution[1] + 3) % 6, True)  # a mistake
        g.hint(True)
        g.cur = [4, 5]
        g.t0 -= 75
        self.assertTrue(M.save_session(g, "level"))
        g2, mode = M.load_session()
        self.assertEqual(mode, "level")
        self.assertEqual(g2.grid, g.grid)
        self.assertEqual(g2.cur, [4, 5])
        self.assertEqual((g2.lives, g2.max_lives, g2.hints, g2.mistakes), (2, 3, 1, 1))
        self.assertEqual(g2.p.level, (6, "medium", 2))
        self.assertEqual(g2.p.regions, p.regions)
        self.assertAlmostEqual(g2.elapsed(), g.elapsed(), delta=1.0)
        M.clear_session()
        self.assertIsNone(M.load_session())

    def test_corrupt_session(self):
        os.makedirs(os.path.dirname(M.session_path()))
        for junk in ("", "[]", "{}", '{"puzzle": {"n": 2}}'):
            with open(M.session_path(), "w") as f:
                f.write(junk)
            self.assertIsNone(M.load_session(), junk)

    def test_stats_migrate_from_purrgrid(self):
        M.write_json(M.stats_path("purrgrid"), {"8x8-easy": {"solved": 4, "best": 61.0}})
        self.assertEqual(M.load_stats()["8x8-easy"]["solved"], 4)
        M.save_stats({"8x8-easy": {"solved": 5, "best": 60.0}})
        self.assertEqual(M.load_stats()["8x8-easy"]["solved"], 5)  # new file wins

    def test_stats_lines(self):
        st = {"8x8-easy": {"solved": 2, "best": 75.0}, "daily": {"2026-01-01": 80.0},
              "levels": {"10x10-hard": {"1": {"best": None}}, "8x8-hard": {"1": {"best": 9.0}}}}
        lines = M.stats_lines(st)
        self.assertIn("8x8-easy       solved    2   best 01:15", lines)
        self.assertTrue(lines.index(next(l for l in lines if "8x8-hard" in l)) <
                        lines.index(next(l for l in lines if "10x10-hard" in l)))


class TestCommandLine(TempDataDir):
    def run_cli(self, *args):
        env = dict(os.environ, XDG_DATA_HOME=self.tmp)
        return subprocess.run([sys.executable, os.path.join(ROOT, "meshcheck.py")] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                              universal_newlines=True, timeout=120)

    def test_version(self):
        r = self.run_cli("--version")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("meshcheck "))

    def test_print_then_load_format(self):
        r = self.run_cli("--print", "2", "-s", "6", "-d", "easy", "--seed", "7")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(M.parse_puzzles(r.stdout)), 2)

    def test_stats_empty(self):
        r = self.run_cli("--stats")
        self.assertIn("No puzzles solved yet", r.stdout)

    def test_bad_level(self):
        self.assertNotEqual(self.run_cli("--level", "3", "-s", "4").returncode, 0)
        self.assertNotEqual(self.run_cli("--level", "99").returncode, 0)


@unittest.skipIf(sys.platform.startswith("win"), "install.sh is for Linux and macOS")
class TestInstallScript(TempDataDir):
    def sh(self, *args, **env):
        e = dict(os.environ, PREFIX=os.path.join(self.tmp, "prefix"), XDG_DATA_HOME=self.tmp)
        e.update(env)
        return subprocess.run(["sh", os.path.join(ROOT, "install.sh")] + list(args), env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def test_install_run_uninstall(self):
        target = os.path.join(self.tmp, "prefix", "bin", "meshcheck")
        r = self.sh(PYTHON=sys.executable)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.access(target, os.X_OK))
        with open(target) as f:
            self.assertTrue(f.readline().startswith("#!/"))
        out = subprocess.run([target, "--version"], stdout=subprocess.PIPE, universal_newlines=True)
        self.assertTrue(out.stdout.startswith("meshcheck "))
        r = self.sh("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(target))

    def test_skips_python_without_curses(self):
        bad = os.path.join(self.tmp, "badpython")
        with open(bad, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(bad, 0o755)
        r = self.sh(PYTHON=bad)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(bad, r.stdout)


if __name__ == "__main__":
    unittest.main()
