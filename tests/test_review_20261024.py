"""Review findings 2026-10-24: a step identity must not change between processes; a punctuated 'no' in a use matrix is still a no."""
import os
import subprocess
import sys
import unittest

from vega_harness import use_gate as ug

PROG = ("from vega_harness.records import code_hash\n"
        "def step(ctx):\n"
        "    return {'ok': ctx.get('k') in {'alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta'}}\n"
        "print(code_hash(step))\n")


class Review(unittest.TestCase):
    def test_a_step_with_a_set_literal_has_the_same_identity_in_every_process(self):
        seen = set()
        for seed in ("1", "2", "3", "4"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            seen.add(subprocess.run([sys.executable, "-c", PROG], capture_output=True, text=True, env=env, check=True).stdout.strip())
        self.assertEqual(len(seen), 1)

    def test_a_different_set_literal_is_still_a_different_identity(self):
        from vega_harness.records import code_hash

        def a(c):
            return c in {"x", "y"}

        def b(c):
            return c in {"x", "z"}
        self.assertNotEqual(code_hash(a), code_hash(b))

    def test_a_punctuated_no_in_the_matrix_forbids_a_personal_build(self):
        for v in ("no", "No.", "no: licence forbids", "NO - read only", "never", "Not allowed", "forbidden", "denied (licence)", ""):
            self.assertTrue(ug.personal_forbidden({"personal_build": v}), v)
        for v in ("read in quarantine only", "contained use", "yes", "none"):
            self.assertFalse(ug.personal_forbidden({"personal_build": v}), v)


if __name__ == "__main__":
    unittest.main()
