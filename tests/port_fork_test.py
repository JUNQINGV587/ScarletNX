# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the ScarletNX contributors
"""scripts/check-port-fork.py on checkouts of its own: a complete one passes, a marker a merge dropped is named with
its file and its patch, a file it cannot read is exit 2, and the checkout of this repository passes"""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/check-port-fork.py'
spec = importlib.util.spec_from_file_location('check_port_fork', SCRIPT)
cpf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpf)


class Tree:
    """a temporary checkout that holds the marker of each patch"""

    def __init__(self):
        self.folder = tempfile.TemporaryDirectory(prefix='snx-port-fork-test-')
        self.root = Path(self.folder.name) / 'checkout'
        self.root.mkdir()
        body = {}
        for name, marker, _ in cpf.PATCHES:
            body.setdefault(name, '')
            body[name] += f'/* the patch */ {marker}\n'
        for name, text in body.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def run(self):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root)],
                              capture_output=True, text=True)


class CheckPortForkTest(unittest.TestCase):
    def setUp(self):
        self.tree = Tree()
        self.addCleanup(self.tree.folder.cleanup)

    def test_a_complete_checkout_passes(self):
        result = self.tree.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')

    def test_a_dropped_marker_names_its_file_and_patch(self):
        name, marker, what = cpf.PATCHES[0]
        (self.tree.root / name).write_text('/* the merge took the upstream file */\n')
        result = self.tree.run()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f'{name}: no {marker!r}', result.stdout)
        self.assertIn(what, result.stdout)
        self.assertIn('fork patches are missing', result.stdout)

    def test_a_file_it_cannot_read_is_exit_2(self):
        (self.tree.root / cpf.PATCHES[-1][0]).unlink()
        result = self.tree.run()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('No such file', result.stdout)

    def test_this_repository_passes(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
