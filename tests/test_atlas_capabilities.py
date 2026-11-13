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
    def test_every_listed_call_matches_method_and_path_in_atlas_client(self):
        method_of = {"get": "GET", "get_all_pages": "GET", "patch": "PATCH"}
        client_calls = {(method_of[m], norm(path)) for m, path in
                        re.findall(r'self\._(get_all_pages|get|patch)\(\s*f?"(/[^"\s]*)"', CLIENT)}
        self.assertIn(("PATCH", "/groups/{}/clusters/{}"), client_calls)
        listed = re.findall(r"'(GET|PATCH|POST|DELETE) (/[^ ']+)", CAPS)
        self.assertGreater(len(listed), 10)
        missing = sorted({(m, p) for m, p in listed if (m, norm(p)) not in client_calls})
        self.assertEqual(missing, [], f"method + path not called by backend/atlas_client.py: {missing}")

    def test_screens_list_the_calls_their_guards_and_metrics_make(self):
        def calls(screen):
            block = re.search(rf"\n  {screen}: \{{(.*?)\n  \}},", CAPS, re.S).group(1)
            return block
        # explain / createIndex check the cluster identity (get_cluster) before touching the driver
        for screen in ("indexes", "profiler"):
            self.assertIn("'GET /groups/{groupId}/clusters/{clusterName} ", calls(screen), screen)
        # live metrics read the disk partition measurements
        self.assertIn("/disks/{partitionName}/measurements", calls("scale"))

    def test_every_screen_has_an_entry(self):
        screens = re.findall(r"\{ id: '(\w+)'", APP)
        self.assertGreaterEqual(len(screens), 7)
        for screen in screens:
            self.assertRegex(CAPS, rf"\n  {screen}: \{{", screen)


if __name__ == "__main__":
    unittest.main()
