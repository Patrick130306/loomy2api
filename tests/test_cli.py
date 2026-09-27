"""CLI wiring tests — cheap regression net for the argument/plumbing layer."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from loomy2api import cli


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg_file = self.dir / "config.json"
        self.cfg_file.write_text(json.dumps({
            "accounts_file": str(self.dir / "accounts.json"),
            "log_dir": str(self.dir / "logs"),
            "sessions_from_client": False,
            "log_console": False,
        }), encoding="utf-8")
        (self.dir / "accounts.json").write_text(json.dumps({"accounts": [
            {"name": "a", "session": "s1",
             "expireAt": int(time.time()) + 14 * 86400}]}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["-c", str(self.cfg_file), *argv])
        return code, buf.getvalue()

    def test_accounts_lists_pool(self):
        code, out = self.run_cli(["accounts"])
        self.assertEqual(code, 0)
        self.assertIn("a", out)
        self.assertIn("策略", out)

    def test_serve_passes_gateway_through(self):
        """Regression: cmd_serve must hand a working gateway to serve()."""
        calls = {}
        original = cli.serve

        def fake_serve(cfg, gateway=None):
            calls["gateway"] = gateway or cli.Gateway(cfg)
            calls["port"] = cfg["port"]

        cli.serve = fake_serve
        try:
            code, _out = self.run_cli(["serve", "--port", "19999"])
        finally:
            cli.serve = original
        self.assertEqual(code, 0)
        self.assertEqual(calls["port"], 19999)
        self.assertEqual(len(calls["gateway"].pool.accounts), 1)

    def test_add_and_remove(self):
        code, out = self.run_cli(["add", "b", "--phone", "13800000000",
                                  "--password", "pw", "--no-login"])
        self.assertEqual(code, 0)
        self.assertIn("已添加账号 b", out)
        code, out = self.run_cli(["remove", "b", "-y"])
        self.assertEqual(code, 0)
        self.assertIn("已删除 b", out)

    def test_quota_reports_total(self):
        code, out = self.run_cli(["quota"])
        self.assertEqual(code, 0)
        self.assertIn("合计可用积分", out)

    def test_help_without_command(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main([])
        self.assertEqual(code, 0)
        self.assertIn("serve", buf.getvalue())

    def test_cli_survives_legacy_console_encoding(self):
        """Windows CI runs with a legacy codepage (cp1252), and every message
        we print is UTF-8 — the CLI must force UTF-8 instead of dying."""
        import os
        import subprocess
        import sys

        env = dict(os.environ, PYTHONIOENCODING="cp1252",
                   LOOMY_ACCOUNTS_FILE=str(self.dir / "accounts.json"),
                   LOOMY_LOG_DIR=str(self.dir / "logs"))
        for args in (["--help"], ["-c", str(self.cfg_file), "accounts"]):
            proc = subprocess.run([sys.executable, "-m", "loomy2api", *args],
                                  capture_output=True, env=env,
                                  cwd=str(Path(__file__).resolve().parent.parent))
            self.assertEqual(
                proc.returncode, 0,
                f"{args} failed: {proc.stderr.decode('utf-8', 'replace')[:400]}")
            self.assertNotIn(b"UnicodeEncodeError", proc.stderr)


if __name__ == "__main__":
    unittest.main()
