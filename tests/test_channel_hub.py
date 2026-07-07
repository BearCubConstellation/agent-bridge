import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from adapters.chat_runtime import HermesChannelAdapter, OpenClawChannelAdapter
from rooms import ensure_room


class ChatAdapterTests(unittest.TestCase):
    def test_openclaw_capability_requires_hook_url(self):
        cap = OpenClawChannelAdapter().capability({"adapter": {"type": "openclaw_channel", "config": {}}})
        self.assertFalse(cap["configured"])
        self.assertFalse(cap["automatic"])

    def test_hermes_capability_accepts_hook_url(self):
        cap = HermesChannelAdapter().capability({"adapter": {"type": "hermes_channel", "config": {"url": "http://127.0.0.1:9000"}}})
        self.assertTrue(cap["configured"])
        self.assertTrue(cap["automatic"])


if __name__ == "__main__":
    unittest.main()
