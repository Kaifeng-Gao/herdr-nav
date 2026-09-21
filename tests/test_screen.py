"""Tests for retained terminal frames and coherent presentation."""

import unittest

import pyte

from herdrnav.contracts import Frame
from herdrnav.screen import FramePresentation, PresentationTimeout, TerminalImage


class TerminalImageTests(unittest.TestCase):
    def test_full_and_incremental_frames_form_one_snapshot(self) -> None:
        image = TerminalImage()
        image.update(Frame(20, 4, True, b"\x1b[2J\x1b[Hhello "))
        image.update(Frame(20, 4, False, b"world"))

        snapshot = image.snapshot()

        self.assertIn(b"hello", snapshot)
        self.assertIn(b"world", snapshot)
        self.assertIn(b"\x1b[", snapshot)


class FramePresentationTests(unittest.TestCase):
    def test_initial_frame_waits_for_settled_matching_viewport(self) -> None:
        presentation = FramePresentation((80, 24), 0.0)
        update = presentation.update(
            (Frame(70, 20, True, b"\x1b[2J\x1b[HWRONG SIZE"),),
            0.05,
        )
        self.assertIsNone(update.output)
        self.assertFalse(presentation.update((), 0.40).ready)

        presentation.update(
            (Frame(80, 24, True, b"\x1b[2J\x1b[HREADY"),),
            0.45,
        )
        self.assertIsNone(presentation.update((), 0.50).output)
        rendered = presentation.update((), 0.70).output

        self.assertIsNotNone(rendered)
        self.assertIn(b"READY", rendered or b"")
        self.assertNotIn(b"WRONG SIZE", rendered or b"")

    def test_resize_suppresses_intermediate_text_until_settled(self) -> None:
        presentation = FramePresentation((80, 24), 0.0)
        presentation.update((Frame(80, 24, True, b"OLD"),), 0.01)
        self.assertIsNotNone(presentation.update((), 0.30).output)

        self.assertTrue(presentation.resize((100, 30), 0.40))
        presentation.update((Frame(100, 30, True, b"INTERMEDIATE"),), 0.45)
        presentation.update((Frame(100, 30, True, b"LATEST"),), 0.55)
        rendered = presentation.update((), 0.80).output

        self.assertIsNotNone(rendered)
        self.assertIn(b"LATEST", rendered or b"")
        self.assertNotIn(b"INTERMEDIATE", rendered or b"")

    def test_incremental_frame_emits_a_state_complete_snapshot(self) -> None:
        presentation = FramePresentation((20, 4), 0.0)
        presentation.update(
            (Frame(20, 4, True, b"\x1b[31mA"),),
            0.01,
        )
        initial = presentation.update((), 0.30).output
        incremental = presentation.update(
            (Frame(20, 4, False, b"B"),),
            0.40,
        ).output

        self.assertIsNotNone(initial)
        self.assertIsNotNone(incremental)
        self.assertNotEqual(incremental, b"B")
        physical = pyte.Screen(20, 4)
        stream = pyte.ByteStream(physical)
        stream.feed((initial or b"") + (incremental or b""))
        self.assertEqual(physical.buffer[0][1].data, "B")
        self.assertEqual(physical.buffer[0][1].fg, "red")

    def test_resize_without_a_matching_frame_times_out(self) -> None:
        presentation = FramePresentation((80, 24), 0.0)
        presentation.update((Frame(80, 24, True, b"READY"),), 0.01)
        self.assertTrue(presentation.update((), 0.30).ready)
        presentation.resize((100, 30), 0.40)

        with self.assertRaises(PresentationTimeout):
            presentation.update((), 15.41)


if __name__ == "__main__":
    unittest.main()
