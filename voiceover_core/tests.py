import tempfile
import unittest
from pathlib import Path

from voiceover_core.alignment import find_take_candidates, tokenize
from voiceover_core.diagnostics import DiagnosticOptions, diagnose_segments
from voiceover_core.exports import create_clips_archive, create_sidecar_exports
from voiceover_core.subtitles import parse_script


class SubtitleTests(unittest.TestCase):
    def test_srt_preserves_explicit_intervals(self):
        entries = parse_script(
            "1\n00:00:01,250 --> 00:00:02,500\nFirst phrase\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nSecond phrase\n",
            "captions.srt",
        )
        self.assertEqual((entries[0]["video_start"], entries[0]["video_end"]), (1.25, 2.5))
        self.assertEqual(entries[1]["text"], "Second phrase")

    def test_vtt_and_csv_are_auto_detected(self):
        vtt = parse_script("WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n", "captions.vtt")
        csv_entries = parse_script("start;end;text\n0;1.5;Hello\n1.5;3;World\n", "captions.csv")
        self.assertEqual(vtt[0]["text"], "Hello")
        self.assertEqual(csv_entries[1]["video_start"], 1.5)


class DiagnosticTests(unittest.TestCase):
    def test_problem_reasons_are_independent(self):
        problems = diagnose_segments([
            {"status": "ok", "audio_start": 0, "audio_end": 1, "score": .9, "speed": 1},
            {"status": "low_confidence", "audio_start": .8, "audio_end": 2, "score": .2, "speed": 1.2},
        ], DiagnosticOptions(speed_warning=1.12))
        self.assertEqual(problems[0], [])
        self.assertEqual(set(problems[1]), {"low_confidence", "speed", "overlap"})


class TakeAlignmentTests(unittest.TestCase):
    def test_repeated_phrase_produces_multiple_take_candidates(self):
        entries = [{"id": 1, "text": "hello world", "tokens": tokenize("hello world")}]
        words = [
            {"word": "hello", "start": 0, "end": .3},
            {"word": "world", "start": .3, "end": .7},
            {"word": "hello", "start": 1.2, "end": 1.5},
            {"word": "world", "start": 1.5, "end": 1.9},
        ]
        matches = find_take_candidates(entries, words)
        self.assertEqual(len(matches[1]), 2)
        self.assertEqual(matches[1][0]["audio_start"], 0)


class ExportTests(unittest.TestCase):
    def test_sidecars_are_immediate_and_clips_archive_is_on_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clip = root / "001.wav"
            clip.write_bytes(b"RIFF-test")
            paths = create_sidecar_exports(
                root / "output",
                [{"id": 1, "video_start": 0, "video_end": 1.5, "text": "Hello"}],
                {1: clip},
            )
            self.assertEqual(set(paths), {"srt", "csv", "fcpxml"})
            self.assertTrue(all(path.exists() for path in paths.values()))
            self.assertFalse((root / "output" / "voiceover-clips.zip").exists())
            archive = create_clips_archive(root / "output", {1: clip})
            self.assertTrue(archive.exists())


class ArchitectureTests(unittest.TestCase):
    def test_core_does_not_import_django_or_studio_models(self):
        root = Path(__file__).parent
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in root.glob("*.py")
            if path.name != "tests.py"
        )
        self.assertNotIn("import django", source)
        self.assertNotIn("from django", source)
        self.assertNotIn("studio.models", source)


if __name__ == "__main__":
    unittest.main()
