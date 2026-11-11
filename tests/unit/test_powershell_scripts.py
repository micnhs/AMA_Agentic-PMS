"""Static checks of the PowerShell scripts.

The build sandbox has no PowerShell; the CI job "Lint" parses every script with pwsh. These tests pin the
Windows lessons of the first run on a PowerShell 5.1 console (cycle 7).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


class PowerShellScriptTests(unittest.TestCase):
    def test_every_script_requires_powershell_7_2(self) -> None:
        files = sorted(SCRIPTS.glob("*.ps1"))
        self.assertGreaterEqual(len(files), 9)
        for path in files:
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(lines[0].strip(), "#Requires -Version 7.2", path.name)

    def test_version_guard_in_common_stops_the_calling_script(self) -> None:
        text = (SCRIPTS / "lib" / "Common.ps1").read_text(encoding="utf-8")
        match = re.search(r"if \(\$PSVersionTable\.PSVersion -lt \[version\]'7\.2'\) \{(.*?)\n\}", text, re.S)
        self.assertIsNotNone(match)
        self.assertIn("throw", match.group(1))
        self.assertNotRegex(match.group(1), r"\bexit\b")

    def test_python_probe_passes_no_double_quotes(self) -> None:
        text = (SCRIPTS / "lib" / "Common.ps1").read_text(encoding="utf-8")
        match = re.search(r"\$probe = '([^']*)'", text)
        self.assertIsNotNone(match)
        self.assertNotIn('"', match.group(1))

    def test_readme_tells_windows_users_how_to_run_the_scripts(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quick = readme[readme.index("## Offline quick start"):readme.index("## Deploy to Azure")]
        for needle in ("pwsh", "winget install --id Microsoft.PowerShell", "Unblock-File", "#requires",
                       "python -m pms serve --seed"):
            self.assertIn(needle, quick)


if __name__ == "__main__":
    unittest.main()
