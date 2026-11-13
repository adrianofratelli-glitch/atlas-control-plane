"""The "what this screen demonstrates" panel lists real calls.

frontend/src/atlas-capabilities.js names, per screen, the Atlas Admin API v2 paths the
server calls. Every listed path must be a path literal in backend/atlas_client.py, and
every screen in the navigation must have an entry, so the panel cannot drift from the code.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPS = (ROOT / "frontend" / "src" / "atlas-capabilities.js").read_text(encoding="utf-8")
CLIENT = (ROOT / "backend" / "atlas_client.py").read_text(encoding="utf-8")
APP = (ROOT / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")

PLACEHOLDER = re.compile(r"\{[^}]*\}")


def norm(path: str) -> str:
    return PLACEHOLDER.sub("{}", path.split("?")[0].strip())


class AtlasCapabilitiesTests(unittest.TestCase):
    def test_every_listed_admin_api_path_is_called_by_atlas_client(self):
        client_paths = {norm(m) for m in re.findall(r'f?"(/[^"\s]*)"', CLIENT)}
        listed = re.findall(r"'(?:GET|PATCH|POST|DELETE) (/[^ ']+)", CAPS)
        self.assertGreater(len(listed), 10)
        missing = sorted({p for p in listed if norm(p) not in client_paths})
        self.assertEqual(missing, [], f"paths not called by backend/atlas_client.py: {missing}")

    def test_every_screen_has_an_entry(self):
        screens = re.findall(r"\{ id: '(\w+)'", APP)
        self.assertGreaterEqual(len(screens), 7)
        for screen in screens:
            self.assertRegex(CAPS, rf"\n  {screen}: \{{", screen)


if __name__ == "__main__":
    unittest.main()
