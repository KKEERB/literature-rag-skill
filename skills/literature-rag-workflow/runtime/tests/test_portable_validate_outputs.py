"""The local validation CLI uses only an explicit local paper directory."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / 'scripts' / 'validate_outputs.py'

class PortableValidationTest(unittest.TestCase):
    def test_local_validation_requires_workspace_and_has_no_library_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.pop('LITERATURE_RAG_HOME', None)
            result = subprocess.run([sys.executable, str(CLI), '--paper', tmp], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('LITERATURE_RAG_HOME', result.stderr)
            help_result = subprocess.run([sys.executable, str(CLI), '--help'], env=env, capture_output=True, text=True)
            self.assertNotIn('--all', help_result.stdout)
            self.assertNotIn('--zotero-storage', help_result.stdout)

    def test_reports_symlink_outside_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'; workspace.mkdir()
            external = root / 'external'; external.mkdir()
            try: (workspace / 'reports').symlink_to(external, target_is_directory=True)
            except OSError: self.skipTest('symlink unavailable')
            result = subprocess.run([sys.executable, str(CLI), '--paper', str(root), '--workspace', str(workspace)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('workspace reports must stay inside the workspace', result.stderr)

if __name__ == '__main__': unittest.main()
