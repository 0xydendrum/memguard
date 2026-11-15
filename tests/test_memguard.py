"""memguard tests (Windows). Run:  python -m unittest discover -s tests -v"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from memguard import cli


class Plan(unittest.TestCase):
    """The admission rule: free RAM - reserve >= ram + max(0, vram - free VRAM)."""

    def setUp(self):
        self._ram, self._vram = cli.ram, cli.vram

    def tearDown(self):
        cli.ram, cli.vram = self._ram, self._vram

    def machine(self, free_ram, total_ram=32.0, free_vram=10.0, total_vram=12.0):
        cli.ram = lambda: (free_ram, total_ram, 40.0, 60.0)
        cli.vram = lambda: (free_vram, total_vram)

    def test_fits_without_spill(self):
        self.machine(free_ram=20)
        p = cli.plan(2, 4, 6, True)
        self.assertEqual(p["spill"], 0)
        self.assertEqual(p["need"], 2)
        self.assertTrue(p["fits"])

    def test_vram_shortfall_counts_as_ram(self):
        self.machine(free_ram=20, free_vram=7)
        p = cli.plan(2, 10, 6, True)
        self.assertAlmostEqual(p["spill"], 3)
        self.assertAlmostEqual(p["need"], 5)
        self.assertTrue(p["fits"])  # room 14 >= 5

    def test_waits_when_spill_does_not_fit(self):
        self.machine(free_ram=10, free_vram=7)
        p = cli.plan(2, 10, 6, True)  # room 4 < need 5
        self.assertFalse(p["fits"])
        self.assertFalse(p["never"])

    def test_no_spill_flag_requires_free_vram(self):
        self.machine(free_ram=30, free_vram=7)
        self.assertFalse(cli.plan(0, 10, 6, False)["fits"])
        self.assertTrue(cli.plan(0, 20, 6, False)["never"])  # bigger than the whole card

    def test_never_fits_bigger_than_ram(self):
        self.machine(free_ram=20)
        self.assertTrue(cli.plan(30, 0, 6, True)["never"])

    def test_never_fits_spill_over_half_of_ram(self):
        self.machine(free_ram=31, total_ram=32, free_vram=0)
        self.assertTrue(cli.plan(0, 17, 6, True)["never"])  # Windows caps shared GPU memory at half of RAM

    def test_unreadable_vram_assumes_none_free(self):
        cli.ram = lambda: (20.0, 32.0, 40.0, 60.0)
        cli.vram = lambda: (None, None)
        self.assertEqual(cli.plan(0, 4, 6, True)["spill"], 4)


class StateAndLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("MEMGUARD_STATE")
        os.environ["MEMGUARD_STATE"] = self.tmp.name

    def tearDown(self):
        if self._env is None:
            os.environ.pop("MEMGUARD_STATE", None)
        else:
            os.environ["MEMGUARD_STATE"] = self._env
        self.tmp.cleanup()

    def test_state_dir_follows_env(self):
        self.assertEqual(cli.state_dir(), Path(self.tmp.name))

    def test_lock_is_exclusive(self):
        a, b = cli.Lock("t"), cli.Lock("t")
        self.assertTrue(a.try_acquire())
        self.assertFalse(b.try_acquire())
        self.assertFalse(b.is_free())
        a.release()
        self.assertTrue(b.try_acquire())
        b.release()


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, MEMGUARD_STATE=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def mg(self, *args, **kw):
        return subprocess.run([sys.executable, "-m", "memguard", *args], env=self.env, capture_output=True,
                              text=True, timeout=120, **kw)

    def test_version_credits_author(self):
        r = self.mg("--version")
        self.assertIn("0xydendrum", r.stdout + r.stderr)

    def test_exit_code_passes_through(self):
        r = self.mg("--", sys.executable, "-c", "import sys; sys.exit(7)")
        self.assertEqual(r.returncode, 7, r.stderr)

    def test_never_fitting_job_exits_3(self):
        r = self.mg("--no-lock", "--ram", "100000", "--", sys.executable, "-c", "print('should not run')")
        self.assertEqual(r.returncode, 3)
        self.assertNotIn("should not run", r.stdout)

    def test_clean_job_reports_no_leftovers(self):
        r = self.mg("--no-lock", "--", sys.executable, "-c", "print(1)")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("leftover", r.stderr)

    def test_abandoned_child_is_named_and_stopped(self):
        child = ("import subprocess, sys; "
                 "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])")
        r = self.mg("--no-lock", "--", sys.executable, "-c", child)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stderr, r"stopping 1 leftover process\(es\) the job left running: python")

    def test_killing_memguard_kills_grandchildren(self):
        pidfile = Path(self.tmp.name) / "grandchild.pid"
        child = ("import subprocess, sys, time; "
                 "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
                 f"open(r'{pidfile}', 'w').write(str(g.pid)); time.sleep(120)")
        proc = subprocess.Popen([sys.executable, "-m", "memguard", "--no-lock", "--", sys.executable, "-c", child],
                                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 60
            while not (pidfile.exists() and pidfile.read_text().strip()) and time.time() < deadline:
                time.sleep(0.2)
            grandchild = int(pidfile.read_text())
            self.assertIsNotNone(cli.proc_start(grandchild), "grandchild should be running")
            proc.kill()  # hard kill, like closing the terminal or a task manager kill
            proc.wait(timeout=30)
            deadline = time.time() + 15
            while cli.proc_start(grandchild) is not None and time.time() < deadline:
                time.sleep(0.2)
            self.assertIsNone(cli.proc_start(grandchild), "grandchild outlived memguard")
        finally:
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
