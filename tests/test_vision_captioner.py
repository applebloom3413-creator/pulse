"""
Tests for image captioning on non-Anthropic personas and provider.vision: false
(core/engine.py). Uses a bare Engine with fake models — no API
calls, no real persona data.

Run:
    py -3.14 -m unittest tests.test_vision_captioner -v
"""

import asyncio
import sys
import unittest
from pathlib import Path

_PULSE_ROOT = Path(__file__).resolve().parent.parent
if str(_PULSE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PULSE_ROOT))

from core.engine import PulseEngine  # noqa: E402
from core.llm import LLMClient  # noqa: E402


class _Resp:
    def __init__(self, raw):
        self.raw = raw


class FakeCaptioner(LLMClient):
    """An LLMClient whose chat() takes ONLY messages, like the real one."""
    def __init__(self, text="a possum in a pink hoodie"):
        self.text = text
        self.calls = 0

    def chat(self, messages):                      # one argument on purpose
        self.calls += 1
        assert messages[1]["content"][1]["type"] == "image_url"
        return _Resp(self.text)


class _Stop(Exception):
    pass


class FakeContext:
    def __init__(self):
        self.seen = None

    def _load_conversation(self):
        return []

    def build_conversation_prompt(self, message, history, image_urls=None, **kwargs):
        self.seen = (message, image_urls)
        raise _Stop()                               # we only care what the model would get


class FakeLLM:
    def is_available(self):
        return True


def bare_engine(vision, captioner):
    e = PulseEngine.__new__(PulseEngine)
    e._manual_quiet = False
    e._roll_heartbeat_interval = lambda: 60
    e._mark_activity = lambda: None
    e._check_timeout_state = lambda: None
    e.db = None
    e.skill_registry = None
    e.tools_enabled = False
    e.llm = FakeLLM()
    e.context = FakeContext()
    e.captioner_llm = captioner
    e.vision = vision
    return e


class VisionCaptionerTest(unittest.TestCase):
    def run_turn(self, e, images):
        try:
            asyncio.run(e.handle_message("look!", image_urls=images))
        except _Stop:
            pass
        return e.context.seen

    def test_llmclient_captioner_called_with_one_argument(self):
        e = bare_engine(True, FakeCaptioner())
        self.assertEqual(asyncio.run(e._caption_image("data:image/jpeg;base64,AAAA")),
                         "a possum in a pink hoodie")

    def test_blind_model_gets_description_not_picture(self):
        cap = FakeCaptioner()
        msg, imgs = self.run_turn(bare_engine(False, cap), ["data:image/jpeg;base64,AAAA"])
        self.assertIsNone(imgs)
        self.assertIn("look!", msg)
        self.assertIn("[image: a possum in a pink hoodie]", msg)
        self.assertEqual(cap.calls, 1)

    def test_blind_model_multiple_images_numbered(self):
        msg, imgs = self.run_turn(bare_engine(False, FakeCaptioner()), ["data:a", "data:b"])
        self.assertIn("[image 1:", msg)
        self.assertIn("[image 2:", msg)

    def test_blind_model_without_captioner_gets_gentle_note(self):
        msg, imgs = self.run_turn(bare_engine(False, None), ["data:a"])
        self.assertIsNone(imgs)
        self.assertIn("can't see images", msg)

    def test_seeing_model_still_gets_the_picture(self):
        cap = FakeCaptioner()
        msg, imgs = self.run_turn(bare_engine(True, cap), ["data:a"])
        self.assertEqual(imgs, ["data:a"])
        self.assertEqual(msg, "look!")
        self.assertEqual(cap.calls, 0)        # captioning for memory happens after the reply


if __name__ == "__main__":
    unittest.main()
