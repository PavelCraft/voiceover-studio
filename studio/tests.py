import json
import io
import tempfile
import time
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import Context, Template
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import translation

from .forms import (
    AudioEditorToolForm, PhraseTimingToolForm, PrepareVoiceoverToolForm,
    SplitPhrasesToolForm,
)

from .models import (
    BillingAccount, LocalizationProject, ManualSegmentBoundary, MediaToolInput, MediaToolJob, PhraseTimingProject, ProcessingJob,
    Project, Segment, SegmentReplacement, TakeCandidate, TakePhrase,
    TakeRecording, TakesProject, UsageRecord, VideoAudioMergeJob,
)
from .services.job_queue import execute_job
from .services.media_tools import merge_video_with_audio
from .services.phrase_timing import (
    build_phrases, clamp_phrases_to_duration, process_phrase_timing_project,
    speech_intervals, uncovered_speech_intervals,
)
from .services.reprocessing import (
    build_change_plan, remap_project_annotations,
)
from .services.storage import StorageLimitError, reserve_storage
from .services.tool_processing import _validated_audio_operations, build_docx_bytes, process_media_tool_job
from .services.voiceover import (
    align_entries, merge_overlapping_transcriptions, parse_timecodes, probe_duration, snap_boundaries,
    transcribe_long_audio,
)
from voiceover_core.timing import (
    estimate_speech_duration, export_timed_phrases, format_editable_timing_text, parse_editable_timing_text,
    reconcile_phrase_texts, split_script,
)
from voiceover_core.media import atempo_filters, mp3_codec_arguments, normalize_mp3_bitrate


class MediaFilterTests(SimpleTestCase):
    def test_atempo_supports_slowing_down_and_speeding_up(self):
        self.assertEqual(atempo_filters(0.25), ["atempo=0.5", "atempo=0.500000000"])
        self.assertEqual(
            atempo_filters(0.05),
            ["atempo=0.5", "atempo=0.5", "atempo=0.5", "atempo=0.5", "atempo=0.800000000"],
        )
        self.assertEqual(atempo_filters(1.0), [])
        self.assertEqual(atempo_filters(4.0), ["atempo=2.0", "atempo=2.000000000"])

    def test_audio_editor_allows_a_short_fragment_to_be_stretched_well_beyond_four_times(self):
        operations = _validated_audio_operations(
            [{"type": "speed", "start": 1.0, "end": 1.1, "speed": 0.05}],
            duration=3.0,
        )
        self.assertEqual(operations[0]["speed"], 0.05)

    def test_audio_editor_can_replace_a_tiny_silent_source_with_a_long_pause(self):
        operations = _validated_audio_operations(
            [{"type": "silence", "start": 1.0, "end": 1.015, "duration": 60.0}],
            duration=3.0,
        )
        self.assertEqual(operations[0]["duration"], 60.0)

    def test_audio_editor_can_insert_silence_without_replacing_source_audio(self):
        operations = _validated_audio_operations(
            [{"type": "silence", "start": 1.0, "end": 1.0, "duration": 1.0}],
            duration=3.0,
        )
        self.assertEqual(
            operations[0],
            {"type": "silence", "start": 1.0, "end": 1.0, "duration": 1.0},
        )

    def test_mp3_quality_is_shared_by_every_mp3_export_form(self):
        for form_class in (AudioEditorToolForm, SplitPhrasesToolForm, PrepareVoiceoverToolForm):
            form = form_class()
            self.assertIn("mp3_bitrate", form.fields)
            self.assertEqual(
                [value for value, _label in form.fields["mp3_bitrate"].choices],
                ["128", "192", "256", "320"],
            )
            self.assertGreater(
                list(form.fields).index("mp3_bitrate"),
                list(form.fields).index("output_format"),
            )

    def test_mp3_codec_arguments_validate_and_reuse_the_selected_bitrate(self):
        self.assertEqual(mp3_codec_arguments("320"), ["-c:a", "libmp3lame", "-b:a", "320k"])
        self.assertEqual(normalize_mp3_bitrate("999"), "192")

    def test_overlapping_transcriptions_use_each_word_once_at_the_join(self):
        first = {
            "language": "ru",
            "words": [
                {"word": "первое", "start": 294.0, "end": 294.5},
                {"word": "слово", "start": 296.0, "end": 296.4},
                {"word": "дубликат", "start": 298.0, "end": 298.5},
            ],
            "segments": [{"text": "первое слово дубликат", "start": 294.0, "end": 298.5}],
        }
        second = {
            "language": "ru",
            "words": [
                {"word": "слово", "start": 1.0, "end": 1.4},
                {"word": "второе", "start": 3.0, "end": 3.5},
            ],
            "segments": [{"text": "слово второе", "start": 1.0, "end": 3.5}],
        }

        result = merge_overlapping_transcriptions([first, second], [0.0, 295.0], 600.0)

        self.assertEqual([word["word"] for word in result["words"]], ["первое", "слово", "второе"])
        self.assertEqual(result["text"], "первое слово второе")
        self.assertEqual(result["words"][-1]["start"], 298.0)
        self.assertNotIn("дубликат", result["text"])

    def test_long_transcription_uses_five_minute_chunks_with_five_second_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source.flac", root / "transcription.json"
            source.write_bytes(b"audio")
            progress = []

            def create_chunk(command):
                Path(command[-1]).write_bytes(b"small chunk")

            def recognize(chunk, transcript, **kwargs):
                number = int(Path(chunk).stem)
                return {
                    "language": "ru",
                    "words": [{"word": f"слово{number}", "start": 3.0, "end": 3.4}],
                    "segments": [{"text": f"слово{number}", "start": 3.0, "end": 3.4}],
                }

            with patch("studio.services.voiceover.probe_duration", return_value=960.0), \
                 patch("studio.services.voiceover.run", side_effect=create_chunk) as ffmpeg, \
                 patch("studio.services.voiceover.transcribe", side_effect=recognize):
                result = transcribe_long_audio(
                    source, output, progress=lambda index, total: progress.append((index, total)),
                )

            self.assertEqual(ffmpeg.call_count, 4)
            starts = [call.args[0][call.args[0].index("-ss") + 1] for call in ffmpeg.call_args_list]
            self.assertEqual(starts, ["0.000000", "295.000000", "590.000000", "885.000000"])
            self.assertEqual(progress, [(1, 4), (2, 4), (3, 4), (4, 4)])
            self.assertEqual([word["start"] for word in result["words"]], [3.0, 298.0, 593.0, 888.0])
            self.assertTrue(output.is_file())
            self.assertFalse((root / "transcription-chunks").exists())


class PhraseTimingCoreTests(SimpleTestCase):
    def test_reprocessing_plan_distinguishes_timing_text_and_structure(self):
        original = [
            {"text": "Первая", "start": 0.0, "end": 2.0},
            {"text": "Вторая", "start": 2.0, "end": 4.0},
        ]

        timing = build_change_plan(original, [
            {"text": "Первая", "start": 0.0, "end": 2.5},
            {"text": "Вторая", "start": 2.5, "end": 4.0},
        ])
        text_plan = build_change_plan(original, [
            {"text": "Первая исправлена", "start": 0.0, "end": 2.0},
            {"text": "Вторая", "start": 2.0, "end": 4.0},
        ])
        structure = build_change_plan(original, [
            {"text": "Первая", "start": 0.0, "end": 2.0},
            {"text": "Вставка", "start": 2.0, "end": 2.5},
            {"text": "Вторая", "start": 2.5, "end": 4.0},
        ])
        text_and_timing = build_change_plan(original, [
            {"text": "Первая исправлена", "start": 0.0, "end": 2.5},
            {"text": "Вторая", "start": 2.5, "end": 4.0},
        ])

        self.assertEqual(timing["kind"], "timing")
        self.assertEqual(timing["positions"], [1, 2])
        self.assertEqual(text_plan["kind"], "text")
        self.assertEqual(text_plan["from_position"], 1)
        self.assertEqual(text_and_timing["kind"], "timing")
        self.assertEqual(structure["kind"], "structure")
        self.assertEqual(structure["from_position"], 2)

    def test_recognition_rounding_is_clamped_to_media_duration(self):
        phrases = clamp_phrases_to_duration([
            {
                "id": 1, "text": "Фраза", "start": 17.3, "end": 18.02,
                "end_explicit": True,
                "words": [{"text": "Фраза", "start": 17.3, "end": 18.02}],
            },
        ], 18.0)
        self.assertEqual(phrases[0]["end"], 18.0)
        self.assertEqual(phrases[0]["words"][0]["end"], 18.0)

    def test_automatic_phrases_keep_word_boundaries_and_explicit_ends(self):
        result = build_phrases({
            "words": [
                {"word": "Первая", "start": 1.0, "end": 1.4},
                {"word": " фраза.", "start": 1.45, "end": 2.0},
                {"word": "Вторая", "start": 2.8, "end": 3.2},
                {"word": " фраза", "start": 3.25, "end": 3.8},
            ],
            "segments": [],
        })
        self.assertEqual([item["text"] for item in result], ["Первая фраза.", "Вторая фраза"])
        self.assertEqual(result[0]["start"], 1.0)
        self.assertEqual(result[0]["end"], 2.0)
        self.assertTrue(result[0]["end_explicit"])
        self.assertEqual(len(result[0]["words"]), 2)

    def test_vad_reports_only_voice_not_covered_by_recognized_words(self):
        result = uncovered_speech_intervals(
            [(0.0, 1.0), (2.0, 3.0)],
            [{"start": 0.0, "end": 1.0}],
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "unrecognized_speech")
        self.assertAlmostEqual(result[0]["start"], 2.0)

    def test_silero_probabilities_use_hysteresis_and_ignore_short_noise(self):
        probabilities = [0.0] * 4 + [0.8] * 20 + [0.0] * 8 + [0.8] * 2 + [0.0] * 8
        result = speech_intervals(probabilities, len(probabilities) * 512)
        self.assertEqual(len(result), 1)

    def test_plain_script_is_split_by_lines_and_sentence_endings(self):
        self.assertEqual(
            split_script("Первая фраза. Вторая фраза!\nТретья строка"),
            ["Первая фраза.", "Вторая фраза!", "Третья строка"],
        )

    def test_resegmenting_retains_first_timecode_when_phrase_is_split(self):
        result = reconcile_phrase_texts(
            [{"text": "Первая часть и вторая часть", "start": 3.25}],
            "Первая часть.\nИ вторая часть",
        )
        self.assertEqual(result[0]["start"], 3.25)
        self.assertIsNone(result[1]["start"])

    def test_timed_phrases_export_to_srt(self):
        content = export_timed_phrases([
            {"text": "Первая", "start": 1.25},
            {"text": "Вторая", "start": 3.5},
        ], "srt", duration=8)
        self.assertIn("00:00:01,250 --> 00:00:03,500", content)
        self.assertIn("Вторая", content)

    def test_unmarked_phrases_can_be_merged_into_previous_exported_phrase(self):
        content = export_timed_phrases([
            {"text": "Первая", "start": 1.0},
            {"text": "без отдельного таймкода", "start": None},
            {"text": "Третья", "start": 6.0},
        ], "srt", duration=10, include_unmarked=True)
        self.assertIn("Первая без отдельного таймкода", content)
        self.assertIn("00:00:01,000 --> 00:00:06,000", content)
        self.assertIn("Третья", content)

    def test_leading_unmarked_phrase_blocks_export(self):
        items = [
            {"text": "Вступление без таймкода", "start": None},
            {"text": "Первая размеченная", "start": 2.0},
        ]
        for include_unmarked in (False, True):
            with self.assertRaisesRegex(ValueError, "first phrase"):
                export_timed_phrases(items, "vtt", duration=8, include_unmarked=include_unmarked)

    def test_editable_text_shows_only_assigned_timecodes_and_parses_changes(self):
        editable = format_editable_timing_text([
            {"text": "Первая", "start": 3.696},
            {"text": "Вторая", "start": None},
        ])
        self.assertEqual(editable, "[00:03.696] Первая\nВторая")
        parsed = parse_editable_timing_text("[00:04.125] Первая исправлена\nВторая", duration=12)
        self.assertEqual(parsed[0]["start"], 4.125)
        self.assertIsNone(parsed[1]["start"])

    def test_editable_timecodes_must_stay_in_order(self):
        with self.assertRaisesRegex(ValueError, "ordered"):
            parse_editable_timing_text("[00:08.000] Первая\n[00:02.000] Вторая")

    def test_explicit_phrase_end_survives_editing_and_export(self):
        editable = format_editable_timing_text([
            {"text": "Первая", "start": 1.0, "end": 3.25, "end_explicit": True},
            {"text": "Вторая", "start": 5.0, "end": 8.0, "end_explicit": False},
        ])
        self.assertEqual(editable, "[00:01.000 --> 00:03.250] Первая\n[00:05.000] Вторая")
        parsed = parse_editable_timing_text(editable, duration=8)
        self.assertTrue(parsed[0]["end_explicit"])
        self.assertEqual(parsed[0]["end"], 3.25)
        content = export_timed_phrases(parsed, "srt", duration=8)
        self.assertIn("00:00:01,000 --> 00:00:03,250", content)

    def test_explicit_phrase_end_can_overlap_the_next_phrase(self):
        parsed = parse_editable_timing_text(
            "[00:01.000 --> 00:06.000] Первая\n[00:05.000 --> 00:07.000] Вторая",
            duration=8,
        )
        self.assertEqual(parsed[0]["end"], 6.0)
        self.assertEqual(parsed[1]["start"], 5.0)

    def test_speech_estimate_does_not_treat_domain_dots_as_pauses(self):
        phrase = (
            "В этом видео я покажу несколько способов, как можно заблокировать "
            "раздражающие элементы с помощью этого расширения на примере сайта "
            "mail.ru и почты mail.ru."
        )
        self.assertAlmostEqual(estimate_speech_duration(phrase), 9.18, places=1)


class AlignmentTests(SimpleTestCase):
    def test_cjk_phrase_is_aligned_inside_a_long_transcription_window(self):
        entries = [
            {"tokens": ["拡", "張", "機", "能", "を", "使", "い", "ま", "す"]},
        ]
        words = [
            {"word": character, "start": index * 0.1, "end": (index + 1) * 0.1}
            for index, character in enumerate(
                list("これは前置きです") + list("拡張機能を使います") + list("次の文章です")
            )
        ]

        aligned = align_entries(entries, words)

        self.assertEqual(aligned[0]["status"], "ok")
        self.assertEqual(aligned[0]["audio_start"], 0.8)
        self.assertAlmostEqual(aligned[0]["audio_end"], 1.7)

    def test_missing_phrase_does_not_match_repeated_words_later(self):
        entries = [
            {"tokens": ["alpha"]},
            {"tokens": ["missing", "tail"]},
            {"tokens": ["next", "phrase"]},
            {"tokens": ["missing", "tail", "later"]},
        ]
        words = [
            {"word": "alpha", "start": 0.0, "end": 0.4},
            {"word": "next", "start": 1.0, "end": 1.4},
            {"word": "phrase", "start": 1.5, "end": 2.0},
            {"word": "missing", "start": 8.0, "end": 8.5},
            {"word": "tail", "start": 8.6, "end": 9.0},
            {"word": "later", "start": 9.1, "end": 9.5},
        ]

        aligned = align_entries(entries, words)

        self.assertEqual(aligned[1]["status"], "not_found")
        self.assertIsNone(aligned[1]["audio_start"])
        self.assertEqual(aligned[2]["status"], "ok")
        self.assertEqual(aligned[2]["audio_start"], 1.0)
        self.assertEqual(aligned[3]["status"], "ok")
        self.assertEqual(aligned[3]["audio_start"], 8.0)

    def test_fixed_prefix_is_preserved_and_suffix_starts_after_it(self):
        entries = [
            {"id": 1, "tokens": ["old"]},
            {"id": 2, "tokens": ["future"]},
        ]
        words = [
            {"word": "old", "start": 0.0, "end": 0.4},
            {"word": "future", "start": 10.2, "end": 10.8},
        ]
        fixed = {
            1: {"status": "ok", "audio_start": 1.25, "audio_end": 10.0, "score": 0.91}
        }

        aligned = align_entries(entries, words, fixed_entries=fixed)

        self.assertEqual(aligned[0]["audio_start"], 1.25)
        self.assertEqual(aligned[0]["audio_end"], 10.0)
        self.assertTrue(aligned[0]["_locked"])
        self.assertEqual(aligned[1]["audio_start"], 10.2)


    def test_rejected_pause_does_not_hide_next_valid_pause(self):
        entries = [
            {"audio_start": 386.76, "audio_end": 387.32},
            {"audio_start": 387.32, "audio_end": 387.90},
            {"audio_start": 387.90, "audio_end": 388.44},
        ]
        silences = [
            (387.611, 387.852, 0.241),
            (388.301, 388.416, 0.115),
        ]

        snapped = snap_boundaries(entries, silences)

        self.assertEqual(snapped[0]["audio_end"], 387.611)
        self.assertEqual(snapped[1]["audio_start"], 387.852)
        self.assertEqual(snapped[1]["audio_end"], 388.301)
        self.assertEqual(snapped[2]["audio_start"], 387.90)
        self.assertGreater(
            snapped[1]["audio_end"] - snapped[1]["audio_start"],
            0.4,
        )


    def test_editor_web_audio_state_is_declared(self):
        script = (
            Path(settings.BASE_DIR)
            / "studio"
            / "static"
            / "studio"
            / "segment_editor.js"
        ).read_text(encoding="utf-8")
        for variable in ("audioContext", "audioGain", "playbackEndTimer"):
            self.assertIn(f"let {variable} = null;", script)


    def test_too_short_snapped_fragment_uses_word_timestamps(self):
        entries = [
            {"audio_start": 387.90, "audio_end": 388.44},
            {"audio_start": 388.44, "audio_end": 389.14},
            {"audio_start": 389.14, "audio_end": 389.60},
        ]
        silences = [(388.875, 389.044, 0.169)]

        snapped = snap_boundaries(entries, silences)

        self.assertEqual(snapped[1]["audio_start"], 388.44)
        self.assertEqual(snapped[1]["audio_end"], 389.14)
        self.assertEqual(snapped[1]["boundary_status"], "word")

    def test_timecodes_accept_optional_milliseconds_with_dot_or_comma(self):
        entries = parse_timecodes(
            "00:01.250 - First\n00:02,750 - Second",
            video_duration=5,
        )

        self.assertEqual(entries[0]["video_start"], 1.25)
        self.assertEqual(entries[0]["video_end"], 2.75)
        self.assertEqual(entries[1]["video_start"], 2.75)

    def test_manual_overlapping_boundaries_are_not_snapped_apart(self):
        entries = [
            {
                "status": "manual",
                "audio_start": 10.0,
                "audio_end": 11.2,
            },
            {
                "status": "ok",
                "audio_start": 11.0,
                "audio_end": 12.0,
            },
        ]

        snapped = snap_boundaries(entries, [(10.8, 11.05, 0.25)])

        self.assertEqual(snapped[0]["audio_end"], 11.2)
        self.assertEqual(snapped[1]["audio_start"], 11.0)


class StorageQuotaTests(TransactionTestCase):
    def test_media_limit_counts_files_and_active_reservations(self):
        with tempfile.TemporaryDirectory() as media_root:
            Path(media_root, "existing.bin").write_bytes(b"x" * 200)
            with override_settings(
                MEDIA_ROOT=media_root,
                VOICEOVER_DISK_CHECK_PATH=media_root,
                VOICEOVER_MEDIA_LIMIT_BYTES=500,
                VOICEOVER_DISK_RESERVE_BYTES=0,
            ):
                reserve_storage("first", 100)
                with self.assertRaises(StorageLimitError):
                    reserve_storage("second", 250)

    def test_disk_reserve_is_checked_before_reserving_work(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root,
            VOICEOVER_DISK_CHECK_PATH=media_root,
            VOICEOVER_MEDIA_LIMIT_BYTES=10_000,
            VOICEOVER_DISK_RESERVE_BYTES=800,
        ), patch("studio.services.storage.shutil.disk_usage") as usage:
            usage.return_value.free = 1_000
            with self.assertRaises(StorageLimitError):
                reserve_storage("too-large", 250)


class WorkflowTests(TransactionTestCase):
    def setUp(self):
        self.client = Client(HTTP_HOST="localhost", HTTP_ACCEPT_LANGUAGE="ru")
        self.user = get_user_model().objects.create_user(
            username="owner", password="test-password"
        )
        self.client.force_login(self.user)
        self.project = Project.objects.create(
            owner=self.user,
            audio_file="jobs/test/input/audio/test.webm",
            timecodes_text="00:00 - Тест",
            video_file="jobs/test/input/video/test.mp4",
        )

    def test_navigation_and_progress_page(self):
        with translation.override("ru"):
            source_html = self.client.get("/start/voiceover/").content.decode()
            audio_html = self.client.get(
                f"/project/{self.project.id}/audio/"
            ).content.decode()
            video_html = self.client.get(
                f"/project/{self.project.id}/video/"
            ).content.decode()
            self.assertIn("Выйти из сценария", source_html)
            self.assertIn("Исходное видео", source_html)
            self.assertIn("таймкод", audio_html.lower())
            self.assertIn("Исходное видео", video_html)

            self.project.processing_status = "processing"
            self.project.processing_step = "detecting"
            self.project.save(
                update_fields=["processing_status", "processing_step"]
            )
            summary = self.client.get(
                f"/project/{self.project.id}/"
            ).content.decode()
            self.assertIn("Ищем паузы", summary)
            self.assertIn("processing-progress", summary)

    def test_processing_starts_in_independent_worker(self):
        with patch("studio.views.start_project_worker", return_value=True) as worker:
            response = self.client.post(f"/project/{self.project.id}/process/")
        self.assertEqual(response.status_code, 302)
        worker.assert_called_once_with(self.project)

    def test_processed_project_without_changes_does_not_start_again(self):
        Segment.objects.create(
            project=self.project, position=1, text="Тест", video_start=0,
            video_end=2, audio_file="jobs/test/segments/001.wav", status="ok",
        )
        self.project.processing_status = "complete"
        self.project.pending_reprocessing = {}
        self.project.save(update_fields=["processing_status", "pending_reprocessing"])

        with patch("studio.views.start_project_worker") as worker:
            response = self.client.post(f"/project/{self.project.id}/process/")

        self.assertEqual(response.status_code, 302)
        worker.assert_not_called()
        page = self.client.get(f"/project/{self.project.id}/")
        self.assertNotContains(page, "Распознать и нарезать аудио")
        self.assertNotContains(page, "Обработать заново")

    def test_processing_minutes_are_not_limited_or_recorded(self):
        BillingAccount.objects.create(owner=self.user, monthly_minutes=0, extra_minutes=0)
        job = ProcessingJob.objects.create(
            project=self.project,
            kind=ProcessingJob.KIND_PROCESS,
            status="running",
            payload={},
        )
        with patch("studio.services.job_queue.process_project") as process:
            execute_job(job)
        process.assert_called_once_with(
            self.project,
            reuse_transcription=False,
            realign_from_position=None,
            preserve_following=True,
            timing_only_positions=None,
        )
        job.refresh_from_db()
        self.assertEqual(job.status, "complete")
        self.assertFalse(UsageRecord.objects.filter(job=job).exists())

    def test_timing_only_change_uses_saved_alignment_without_groq_files(self):
        self.project.pending_reprocessing = {
            "kind": "timing", "from_position": 1, "positions": [1],
        }
        self.project.save(update_fields=["pending_reprocessing"])
        Segment.objects.create(
            project=self.project, position=1, text="Тест", video_start=0,
            video_end=2, audio_file="jobs/test/segments/001.wav", status="ok",
        )
        with tempfile.TemporaryDirectory() as media_root:
            work_dir = Path(media_root) / "jobs" / str(self.project.id)
            work_dir.mkdir(parents=True)
            (work_dir / "alignment.json").write_text("[]", encoding="utf-8")
            with override_settings(MEDIA_ROOT=media_root), patch(
                "studio.views.start_project_worker", return_value=True,
            ) as worker:
                response = self.client.post(f"/project/{self.project.id}/process/")

        self.assertEqual(response.status_code, 302)
        worker.assert_called_once_with(self.project, timing_only_positions=[1])

    def test_text_and_structure_changes_reuse_saved_transcription(self):
        with tempfile.TemporaryDirectory() as media_root:
            work_dir = Path(media_root) / "jobs" / str(self.project.id)
            work_dir.mkdir(parents=True)
            transcription = work_dir / "transcription.json"
            transcription.write_text('{"words": []}', encoding="utf-8")
            (work_dir / "source-44100.wav").write_bytes(b"audio")
            self.project.transcription_file = transcription.relative_to(media_root).as_posix()
            self.project.transcription_audio_revision = self.project.audio_revision

            for kind, preserve in (("text", True), ("structure", False)):
                self.project.pending_reprocessing = {
                    "kind": kind, "from_position": 3, "positions": [3],
                }
                self.project.save(update_fields=[
                    "transcription_file", "transcription_audio_revision",
                    "pending_reprocessing",
                ])
                with override_settings(MEDIA_ROOT=media_root), patch(
                    "studio.views.start_project_worker", return_value=True,
                ) as worker:
                    response = self.client.post(f"/project/{self.project.id}/process/")
                self.assertEqual(response.status_code, 302)
                worker.assert_called_once_with(
                    self.project,
                    reuse_transcription=True,
                    realign_from_position=3,
                    preserve_following=preserve,
                )

    def test_phrase_insertion_remaps_manual_boundary_and_single_replacement(self):
        old_phrases = [
            {"text": "Первая"}, {"text": "Вторая"}, {"text": "Третья"},
        ]
        new_phrases = [
            {"text": "Первая"}, {"text": "Вставка"},
            {"text": "Вторая"}, {"text": "Третья"},
        ]
        ManualSegmentBoundary.objects.create(
            project=self.project, position=3, audio_start=3.0, audio_end=4.0,
        )
        SegmentReplacement.objects.create(
            project=self.project, position=2, audio_file="jobs/test/replacement.wav",
        )

        remap_project_annotations(self.project, old_phrases, new_phrases)

        self.assertTrue(self.project.manual_boundaries.filter(position=4).exists())
        self.assertTrue(self.project.segment_replacements.filter(position=3).exists())

    def test_stale_project_cannot_be_rendered(self):
        Segment.objects.create(
            project=self.project, position=1, text="Тест", video_start=0,
            video_end=2, audio_file="jobs/test/segments/001.wav", status="ok",
        )
        self.project.processing_status = "stale"
        self.project.pending_reprocessing = {
            "kind": "text", "from_position": 1, "positions": [1],
        }
        self.project.save(update_fields=["processing_status", "pending_reprocessing"])

        with patch("studio.views.start_project_render_worker") as worker:
            response = self.client.post(f"/project/{self.project.id}/render/")

        self.assertEqual(response.status_code, 302)
        worker.assert_not_called()
        page = self.client.get(f"/project/{self.project.id}/")
        self.assertNotContains(page, "Собрать итоговый результат")

    def test_saved_workflow_steps_are_navigation_and_keep_project_data(self):
        timing = PhraseTimingProject.objects.create(
            owner=self.user,
            video_file=self.project.video_file.name,
            mode=PhraseTimingProject.MODE_AUTOMATIC,
            phrases=[{"id": 1, "text": "Тест", "start": 0.0, "end": 1.0}],
            video_duration=1.0,
        )
        self.project.timing_project = timing
        self.project.save(update_fields=["timing_project"])
        before = {
            "video": self.project.video_file.name,
            "audio": self.project.audio_file.name,
            "timecodes": self.project.timecodes_text,
            "timing": self.project.timing_project_id,
        }

        with translation.override("ru"):
            source = self.client.get(f"/project/{self.project.id}/video/")
            timing_page = self.client.get(f"/project/{self.project.id}/timing/")
            voiceover = self.client.get(f"/project/{self.project.id}/audio/")
            review = self.client.get(f"/project/{self.project.id}/")

        self.assertContains(source, f"/project/{self.project.id}/timing/")
        self.assertContains(timing_page, "Открыть сохранённые таймкоды")
        self.assertContains(timing_page, f"/project/{self.project.id}/audio/")
        self.assertContains(voiceover, "Перейти к проверке перед обработкой")
        self.assertContains(review, "Проверка перед обработкой")
        self.assertContains(review, "Созданы автоматически")
        self.assertNotContains(review, "Введены вручную")

        self.project.refresh_from_db()
        after = {
            "video": self.project.video_file.name,
            "audio": self.project.audio_file.name,
            "timecodes": self.project.timecodes_text,
            "timing": self.project.timing_project_id,
        }
        self.assertEqual(after, before)

    def test_saving_unchanged_completed_steps_does_not_reset_later_progress(self):
        timing = PhraseTimingProject.objects.create(
            owner=self.user,
            video_file=self.project.video_file.name,
            mode=PhraseTimingProject.MODE_AUTOMATIC,
            phrases=[{"id": 1, "text": "Тест", "start": 0.0, "end": 1.0}],
            video_duration=1.0,
        )
        self.project.timing_project = timing
        self.project.processing_status = "complete"
        self.project.output_file = "jobs/test/output/result.m4a"
        self.project.export_manifest = {"srt": "jobs/test/output/result.srt"}
        self.project.save(update_fields=[
            "timing_project", "processing_status", "output_file", "export_manifest",
        ])

        source = self.client.post(
            f"/project/{self.project.id}/video/",
            {
                "output_mode": self.project.output_mode,
                "export_format": self.project.export_format,
                "normalize_loudness": "on",
                "speed_warning_threshold": str(self.project.speed_warning_threshold),
                "retention_days": str(self.project.retention_days),
            },
        )
        self.assertRedirects(
            source,
            f"/project/{self.project.id}/timing/",
            fetch_redirect_response=False,
        )
        voiceover = self.client.post(f"/project/{self.project.id}/audio/", {})
        self.assertRedirects(
            voiceover,
            f"/project/{self.project.id}/",
            fetch_redirect_response=False,
        )

        self.project.refresh_from_db()
        self.assertEqual(self.project.timing_project_id, timing.id)
        self.assertEqual(self.project.timecodes_text, "00:00 - Тест")
        self.assertEqual(self.project.processing_status, "complete")
        self.assertEqual(self.project.output_file, "jobs/test/output/result.m4a")
        self.assertEqual(self.project.export_manifest, {"srt": "jobs/test/output/result.srt"})

    def test_voiceover_workflow_reuses_phrase_timing_editor(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            source = self.client.post(
                "/start/voiceover/",
                {
                    "video_file": SimpleUploadedFile("source.mp4", b"video", content_type="video/mp4"),
                    "output_mode": "audio",
                    "export_format": "m4a",
                    "normalize_loudness": "on",
                    "speed_warning_threshold": "1.12",
                    "retention_days": "30",
                },
            )
            project = Project.objects.exclude(pk=self.project.pk).get()
            self.assertRedirects(source, f"/project/{project.id}/timing/", fetch_redirect_response=False)

            timing_response = self.client.post(
                f"/project/{project.id}/timing/",
                {"timing_action": "create", "mode": "manual", "source_text": "Первая фраза."},
            )
            project.refresh_from_db()
            self.assertIsNotNone(project.timing_project_id)
            self.assertIn(str(project.timing_project_id), timing_response["Location"])
            editor = self.client.get(timing_response["Location"])
            self.assertContains(editor, "phrase-timing-editor")
            self.assertContains(editor, "Перейти к добавлению озвучки")

            timing = project.timing_project
            timing.phrases = [{"id": 1, "text": "Первая фраза.", "start": 0.0, "end": 2.0}]
            timing.video_duration = 2.0
            timing.save(update_fields=["phrases", "video_duration", "updated_at"])
            completed = self.client.get(f"/project/{project.id}/timing/complete/")
            self.assertRedirects(completed, f"/project/{project.id}/audio/", fetch_redirect_response=False)

            audio = self.client.post(
                f"/project/{project.id}/audio/",
                {"audio_file": SimpleUploadedFile("voice.wav", b"audio", content_type="audio/wav")},
            )
            self.assertRedirects(audio, f"/project/{project.id}/", fetch_redirect_response=False)
            project.refresh_from_db()
            self.assertTrue(project.audio_file)
            self.assertIn("Первая фраза", project.timecodes_text)

    def test_automatic_timing_stays_attached_through_review(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            source = self.client.post(
                "/start/voiceover/",
                {
                    "video_file": SimpleUploadedFile("automatic.mp4", b"video", content_type="video/mp4"),
                    "output_mode": "audio",
                    "export_format": "m4a",
                    "normalize_loudness": "on",
                    "speed_warning_threshold": "1.12",
                    "retention_days": "30",
                },
            )
            project = Project.objects.exclude(pk=self.project.pk).get()
            self.assertRedirects(source, f"/project/{project.id}/timing/", fetch_redirect_response=False)

            with patch("studio.forms.probe_has_audio", return_value=True), patch(
                "studio.views.start_phrase_timing_worker", return_value=True,
            ):
                created = self.client.post(
                    f"/project/{project.id}/timing/",
                    {"timing_action": "create", "mode": "automatic", "source_text": ""},
                )
            project.refresh_from_db()
            timing = project.timing_project
            self.assertIsNotNone(timing)
            self.assertEqual(timing.mode, PhraseTimingProject.MODE_AUTOMATIC)
            self.assertIn(str(timing.id), created["Location"])

            timing.phrases = [{
                "id": 1, "text": "Автоматическая фраза.",
                "start": 0.0, "end": 2.0, "end_explicit": True,
            }]
            timing.video_duration = 2.0
            timing.processing_status = "complete"
            timing.save(update_fields=["phrases", "video_duration", "processing_status", "updated_at"])
            completed = self.client.get(f"/project/{project.id}/timing/complete/")
            self.assertRedirects(completed, f"/project/{project.id}/audio/", fetch_redirect_response=False)
            audio = self.client.post(
                f"/project/{project.id}/audio/",
                {"audio_file": SimpleUploadedFile("voice.wav", b"audio", content_type="audio/wav")},
            )
            self.assertRedirects(audio, f"/project/{project.id}/", fetch_redirect_response=False)

            project.refresh_from_db()
            self.assertEqual(project.timing_project_id, timing.id)
            with translation.override("ru"):
                summary = self.client.get(f"/project/{project.id}/")
            self.assertContains(summary, "Созданы автоматически")
            self.assertContains(summary, "Проверка перед обработкой")


    def test_editor_audio_supports_byte_ranges(self):
        with tempfile.TemporaryDirectory() as media_root:
            preview = Path(media_root) / "jobs" / "test" / "source-44100.wav"
            preview.parent.mkdir(parents=True)
            preview.write_bytes(b"0123456789")
            with override_settings(MEDIA_ROOT=media_root), patch(
                "studio.views.ensure_editor_assets",
                return_value={
                    "duration": 1,
                    "source": "jobs/test/source-44100.wav",
                    "audio": "jobs/test/editor-preview.mp3",
                    "peaks": "jobs/test/peaks.json",
                },
            ):
                response = self.client.get(
                    f"/project/{self.project.id}/editor-audio/",
                    HTTP_RANGE="bytes=3-6",
                )
                content = b"".join(response.streaming_content)

            self.assertEqual(response.status_code, 206)
            self.assertEqual(response["Content-Range"], "bytes 3-6/10")
            self.assertEqual(response["Accept-Ranges"], "bytes")
            self.assertEqual(response["Content-Type"], "audio/wav")
            self.assertEqual(content, b"3456")

    def test_segment_card_audio_supports_byte_ranges(self):
        segment = Segment.objects.create(
            project=self.project,
            position=34,
            text="Тест",
            video_start=0,
            video_end=5,
            audio_file="jobs/test/segments/034.wav",
        )
        with tempfile.TemporaryDirectory() as media_root:
            fragment = Path(media_root) / segment.audio_file.name
            fragment.parent.mkdir(parents=True)
            fragment.write_bytes(b"0123456789")
            with override_settings(MEDIA_ROOT=media_root):
                response = self.client.get(
                    f"/project/{self.project.id}/editor-audio/?segment=34",
                    HTTP_RANGE="bytes=3-6",
                )
                content = b"".join(response.streaming_content)

            self.assertEqual(response.status_code, 206)
            self.assertEqual(response["Content-Range"], "bytes 3-6/10")
            self.assertEqual(response["Accept-Ranges"], "bytes")
            self.assertEqual(response["Content-Type"], "audio/wav")
            self.assertEqual(content, b"3456")

    def test_manual_boundary_is_saved_and_reuses_transcript(self):
        Segment.objects.create(project=self.project, position=1, text="Тест", video_start=0, video_end=5, audio_start=1, audio_end=2, status="ok")
        self.project.processing_status = "complete"
        self.project.save(update_fields=["processing_status"])
        with patch("studio.views.ensure_editor_assets", return_value={"duration": 10, "audio": "a.mp3", "peaks": "p.json"}), patch("studio.views.start_project_worker", return_value=True) as worker:
            response = self.client.post(f"/project/{self.project.id}/segments/1/boundary/", data=json.dumps({"audio_start": 1.25, "audio_end": 2.75}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["processing"])
        self.assertIn("status_url", response.json())
        boundary = ManualSegmentBoundary.objects.get(project=self.project, position=1)
        self.assertEqual((boundary.audio_start, boundary.audio_end, boundary.skipped), (1.25, 2.75, False))
        self.assertTrue(worker.call_args.kwargs["reuse_transcription"])
        self.assertEqual(worker.call_args.kwargs["realign_from_position"], 1)

    def test_manual_realign_starts_at_alignment(self):
        from .workers import start_project_worker

        self.project.processing_status = "complete"
        self.project.save(update_fields=["processing_status"])
        with patch("studio.workers._spawn", return_value=123) as spawn:
            self.assertTrue(start_project_worker(self.project, reuse_transcription=True, realign_from_position=7))

        self.project.refresh_from_db()
        self.assertEqual(self.project.processing_status, "queued")
        self.assertEqual(self.project.processing_step, "aligning")
        job = self.project.jobs.get()
        self.assertTrue(job.payload["reuse_transcription"])
        self.assertEqual(job.payload["realign_from_position"], 7)
        self.assertEqual(spawn.call_args.args[0][:2], ["run_voiceover_worker", "--once"])

    def test_full_processing_clears_manual_boundaries_before_spawn(self):
        from .workers import start_project_worker

        ManualSegmentBoundary.objects.create(
            project=self.project, position=1, audio_start=1.0, audio_end=2.0
        )
        SegmentReplacement.objects.create(
            project=self.project, position=1,
            audio_file="jobs/test/replacements/001.wav",
        )
        with patch("studio.workers._spawn", return_value=123) as spawn:
            self.assertTrue(start_project_worker(
                self.project,
                clear_manual_boundaries=True,
                clear_replacements=True,
            ))

        self.assertFalse(self.project.manual_boundaries.exists())
        self.assertFalse(self.project.segment_replacements.exists())
        self.assertNotIn("--reuse-transcription", spawn.call_args.args[0])

    def test_recut_modes_reuse_recognition_and_choose_which_edits_to_keep(self):
        Segment.objects.create(
            project=self.project, position=1, text="Тест", video_start=0,
            video_end=2, audio_file="jobs/test/segments/001.wav", status="ok",
        )
        with tempfile.TemporaryDirectory() as media_root:
            work_dir = Path(media_root) / "jobs" / str(self.project.id)
            work_dir.mkdir(parents=True)
            transcription = work_dir / "transcription.json"
            transcription.write_text('{"words": []}', encoding="utf-8")
            (work_dir / "source-44100.wav").write_bytes(b"audio")
            self.project.transcription_file = transcription.relative_to(media_root).as_posix()
            self.project.transcription_audio_revision = self.project.audio_revision
            self.project.processing_status = "complete"
            self.project.save(update_fields=[
                "transcription_file", "transcription_audio_revision", "processing_status",
            ])

            with override_settings(MEDIA_ROOT=media_root), patch(
                "studio.views.start_project_worker", return_value=True,
            ) as worker:
                page = self.client.get(f"/project/{self.project.id}/")
                self.assertContains(page, "Нарезать аудио на фразы")
                self.assertNotContains(page, "Распознаём речь через Groq")

                self.client.post(
                    f"/project/{self.project.id}/process/",
                    {"processing_action": "recut-preserve"},
                )
                worker.assert_called_once_with(
                    self.project,
                    reuse_transcription=True,
                    realign_from_position=1,
                    preserve_following=False,
                )

                worker.reset_mock()
                self.client.post(
                    f"/project/{self.project.id}/process/",
                    {"processing_action": "recut-reset"},
                )
                worker.assert_called_once_with(
                    self.project,
                    reuse_transcription=True,
                    realign_from_position=1,
                    preserve_following=False,
                    clear_manual_boundaries=True,
                    clear_replacements=True,
                )

    def test_recut_progress_does_not_show_groq_stage(self):
        self.project.processing_status = "queued"
        self.project.processing_step = "aligning"
        self.project.save(update_fields=["processing_status", "processing_step"])
        ProcessingJob.objects.create(
            project=self.project,
            kind=ProcessingJob.KIND_PROCESS,
            status="queued",
            current_step="aligning",
            payload={
                "reuse_transcription": True,
                "realign_from_position": 1,
                "preserve_following": False,
                "timing_only_positions": None,
            },
        )

        response = self.client.get(f"/project/{self.project.id}/")

        self.assertContains(response, "Сопоставляем фразы с распознанными словами")
        self.assertContains(response, "Нарезаем и подгоняем фрагменты")
        self.assertNotContains(response, "Распознаём речь через Groq")

    def test_manual_realign_never_falls_back_to_groq(self):
        from .services.voiceover import ProcessingError, process_project

        with tempfile.TemporaryDirectory() as media_root:
            work_dir = Path(media_root) / "jobs" / str(self.project.pk)
            work_dir.mkdir(parents=True)
            (work_dir / "source-44100.wav").write_bytes(b"prepared")

            with override_settings(MEDIA_ROOT=media_root), patch(
                "studio.services.voiceover.transcribe"
            ) as transcribe:
                with self.assertRaisesRegex(ProcessingError, "transcription is missing"):
                    process_project(self.project, reuse_transcription=True)

        transcribe.assert_not_called()

    def test_phrase_can_be_marked_as_absent(self):
        Segment.objects.create(project=self.project, position=1, text="Тест", video_start=0, video_end=5, status="not_found")
        self.project.processing_status = "complete"
        self.project.save(update_fields=["processing_status"])
        with patch("studio.views.start_project_worker", return_value=True):
            response = self.client.post(f"/project/{self.project.id}/segments/1/boundary/", data=json.dumps({"skipped": True}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        boundary = ManualSegmentBoundary.objects.get(project=self.project, position=1)
        self.assertTrue(boundary.skipped)
        self.assertIsNone(boundary.audio_start)

    @override_settings(VOICEOVER_AUTOSTART_WORKER=False)
    def test_durable_queue_prevents_duplicate_active_jobs(self):
        from .workers import start_project_worker

        self.assertTrue(start_project_worker(self.project))
        self.assertFalse(start_project_worker(self.project))
        job = ProcessingJob.objects.get(project=self.project)
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.payload["reuse_transcription"], False)

    def test_project_page_has_problem_filter_and_single_replacement(self):
        Segment.objects.create(
            project=self.project, position=1, text="Тест", video_start=0,
            video_end=2, audio_start=0.25, audio_end=1.75,
            output_duration=1.5, audio_file="jobs/test/segments/001.wav",
            status="not_found",
        )
        response = self.client.get(f"/project/{self.project.id}/")
        self.assertContains(response, "problem-filter")
        self.assertNotContains(response, "replace-batch")
        self.assertContains(response, 'data-duration="1.5"')
        self.assertContains(response, "Заменить аудио")
        html = response.content.decode()
        self.assertLess(html.index('id="editor-previous"'), html.index('id="editor-phrase"'))
        self.assertLess(html.index('id="editor-phrase"'), html.index('id="editor-next"'))

    def test_editing_review_phrase_updates_saved_timing_project(self):
        timing = PhraseTimingProject.objects.create(
            owner=self.user,
            video_file="tools/phrase-timing/test.mp4",
            source_text="Старый текст\nВторая фраза",
            phrases=[
                {
                    "text": "Старый текст", "start": 0.0, "end": 2.0,
                    "words": [{"word": "Старый", "start": 0.0, "end": 0.5}],
                    "issues": ["low_confidence", "other"],
                },
                {"text": "Вторая фраза", "start": 2.0, "end": 4.0},
            ],
            video_duration=4.0,
        )
        self.project.timing_project = timing
        self.project.processing_status = "complete"
        self.project.save(update_fields=["timing_project", "processing_status"])
        segment = Segment.objects.create(
            project=self.project, position=1, text="Старый текст",
            video_start=0, video_end=2, status="ok",
        )
        Segment.objects.create(
            project=self.project, position=2, text="Вторая фраза",
            video_start=2, video_end=4, status="ok",
        )
        ManualSegmentBoundary.objects.create(
            project=self.project, position=1, audio_start=0.1, audio_end=1.8,
        )

        response = self.client.post(
            f"/project/{self.project.id}/segments/1/text/",
            data=json.dumps({"text": "Исправленный текст"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        segment.refresh_from_db()
        timing.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(segment.text, "Исправленный текст")
        self.assertEqual(timing.phrases[0]["text"], "Исправленный текст")
        self.assertEqual(timing.phrases[0]["words"], [])
        self.assertEqual(timing.phrases[0]["issues"], ["other"])
        self.assertEqual(timing.source_text, "Исправленный текст\nВторая фраза")
        self.assertIn("Исправленный текст", self.project.timecodes_text)
        self.assertIn("Вторая фраза", self.project.timecodes_text)
        self.assertTrue(self.project.manual_boundaries.filter(position=1).exists())
        self.assertEqual(self.project.processing_status, "complete")
        self.assertEqual(self.project.pending_reprocessing, {})

    def test_review_phrase_text_cannot_be_empty(self):
        Segment.objects.create(
            project=self.project, position=1, text="Тест",
            video_start=0, video_end=2, status="ok",
        )
        response = self.client.post(
            f"/project/{self.project.id}/segments/1/text/",
            data=json.dumps({"text": "   "}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)



class MediaToolsTests(TransactionTestCase):
    def setUp(self):
        self.client = Client(
            HTTP_HOST="localhost",
            HTTP_ACCEPT_LANGUAGE="ru",
        )

    def test_tools_and_merge_form_are_available(self):
        tools = self.client.get("/tools/")
        merge = self.client.get("/tools/merge-video-audio/")
        self.assertEqual(tools.status_code, 200)
        self.assertEqual(merge.status_code, 200)
        self.assertContains(tools, "Объединить видео с аудио")
        self.assertContains(tools, "Аудио в текст")
        self.assertContains(tools, "Разметка фраз по времени")
        self.assertContains(tools, "Заменить фрагмент озвучки")
        self.assertContains(tools, "Подготовить озвучку")
        self.assertContains(tools, "Редактировать аудио")
        self.assertContains(merge, "Наложить поверх исходного звука")
        self.assertContains(merge, 'class="toggle"')

    def test_every_mp3_export_page_uses_the_shared_quality_control(self):
        for slug in ("edit-audio", "split-phrases", "prepare-voiceover"):
            page = self.client.get(f"/tools/prototype/{slug}/")
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, 'name="mp3_bitrate"')
            self.assertContains(page, "data-mp3-quality")
            self.assertContains(page, "studio/mp3_quality.js")

    def test_audio_editor_upload_opens_the_editor_job(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with patch("studio.views.start_media_tool_worker", return_value=True) as start:
                response = self.client.post(
                    "/tools/prototype/edit-audio/",
                    {
                        "audio": SimpleUploadedFile("voice.wav", self._wav_bytes(1.0), content_type="audio/wav"),
                        "output_format": "mp3",
                        "mp3_bitrate": "320",
                    },
                )
            job = MediaToolJob.objects.get(kind=MediaToolJob.KIND_EDIT_AUDIO)
            self.assertRedirects(response, f"/tools/audio-editor/{job.id}/", fetch_redirect_response=False)
            self.assertEqual(job.inputs.get().role, "audio")
            self.assertEqual(job.options["mp3_bitrate"], "320")
            start.assert_called_once_with(job)

    def test_audio_editor_renders_waveform_controls_and_queues_export(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={"output_format": "wav"},
                processing_status="complete",
                result_manifest={
                    "stage": "editing", "editor_ready": True, "duration": 3.0,
                    "preview": "tools/jobs/editor-preview.mp3", "peaks": "tools/jobs/waveform-peaks.json",
                },
            )
            MediaToolInput.objects.create(
                job=job, role="audio", media_file=SimpleUploadedFile("voice.wav", self._wav_bytes(3.0)),
            )
            page = self.client.get(f"/tools/audio-editor/{job.id}/")
            self.assertContains(page, 'id="audio-waveform-canvas"')
            self.assertNotContains(page, 'id="audio-speed-handle"')
            self.assertNotContains(page, 'id="audio-apply-speed"')
            self.assertContains(page, 'id="audio-zoom-out"')
            self.assertContains(page, 'id="audio-zoom-in"')
            self.assertContains(page, 'id="audio-track-list"')
            self.assertContains(page, 'data-debug-url=')
            self.assertContains(page, 'data-draft-url=')
            self.assertContains(page, f'/tools/audio-editor/{job.id}/preview/')
            self.assertContains(page, 'id="audio-add-track"')
            self.assertContains(page, 'id="audio-add-silence"')
            self.assertContains(page, "Добавить 1 секунду тишины")
            self.assertContains(page, 'data-silence-position="start"')
            self.assertContains(page, "В начало")
            self.assertContains(page, 'data-silence-position="end"')
            self.assertContains(page, "В конец")
            self.assertContains(page, 'data-remove-url=')
            self.assertContains(page, 'id="audio-apply-merge"')
            self.assertContains(page, 'id="audio-editor-state"')
            self.assertContains(page, 'data-error-merge-first=')
            self.assertContains(page, "Сначала примените объединение.")
            self.assertContains(page, "Удалить фрагмент")
            editor_script = (Path(settings.BASE_DIR) / "studio/static/studio/audio_editor.js").read_text()
            self.assertIn('pause({keepResume: true})', editor_script)
            self.assertIn('cursor = virtualSilenceCursor() ?? sourceToEdited(Math.min(duration, currentSourceTime()))', editor_script)
            self.assertIn('const start = cursor;', editor_script)
            self.assertIn('await seekAudio(position.audio, position.localTime)', editor_script)
            self.assertIn('element.addEventListener("seeked", onSeeked);', editor_script)
            self.assertNotIn('Math.abs(audio.currentTime - position.localTime) > .2', editor_script)
            self.assertNotIn('Math.abs(audio.currentTime - position.localTime) > .25', editor_script)
            self.assertNotIn('cursor >= editedDuration() - .005 ? 0 : cursor', editor_script)
            self.assertIn('setSourcePosition(sourceStart)', editor_script)
            self.assertNotIn('const shouldResume =', editor_script)
            self.assertIn('shell.setPointerCapture(event.pointerId)', editor_script)
            self.assertIn('value == null || value === ""', editor_script)
            self.assertIn('shell.addEventListener("pointerdown", beginWaveformDrag, {capture: true})', editor_script)
            self.assertIn('shell.addEventListener("pointermove", moveWaveformDrag, {capture: true})', editor_script)
            self.assertIn('const MIN_MEDIA_PLAYBACK_RATE = .0625', editor_script)
            self.assertIn('const nextPlaybackBoundary = source =>', editor_script)
            self.assertIn('operation.end < duration - .0005', editor_script)
            self.assertIn('const liveSource = currentSourceTime()', editor_script)
            boundary_scheduler = editor_script.split('function schedulePlaybackBoundary()', 1)[1].split('async function beginVirtualSilence', 1)[0]
            self.assertNotIn('setSourcePosition(boundary)', boundary_scheduler)
            self.assertIn('const silenceCursor = virtualSilenceCursor()', editor_script)
            self.assertIn('resumePlaybackAtBoundary(boundary, token)', editor_script)
            self.assertIn('silenceOperationsForRange(', editor_script)
            self.assertIn('const adjustsGeneratedSilence = draggingHandle.hasGeneratedSilence', editor_script)
            self.assertIn('beginVirtualSilence(silence, sourceToEdited(boundary, false, "left"), token)', editor_script)
            self.assertIn('{type: "silence", start: sourcePosition, end: sourcePosition, duration: 1}', editor_script)
            self.assertIn('const sourcePosition = position === "start" ? 0 : duration', editor_script)
            self.assertIn('const editingLocked = () => !mergeApplied', editor_script)
            self.assertIn('document.getElementById("audio-apply-merge").disabled = mergeApplied', editor_script)
            self.assertIn('const canRemoveTrack = !mergeApplied && group.length === 1', editor_script)
            self.assertIn('cursor = clamp(waveformDrag.cursor - delta * viewDuration', editor_script)
            self.assertIn('playhead.style.left = "50%"', editor_script)
            self.assertIn('const editedDuration = (includeDraft = true) =>', editor_script)
            self.assertIn('const position = setSourcePosition(sourceTarget)', editor_script)
            self.assertIn('const sourceFrom = editedToSource(from, "right")', editor_script)
            self.assertIn('startButton.classList.toggle("primary", selectionStart != null)', editor_script)
            self.assertIn('startMarker.hidden = !hasStart', editor_script)
            self.assertIn('endMarker.addEventListener("pointermove"', editor_script)
            self.assertIn('speedOperationsForRange(', editor_script)
            self.assertIn('audio.playbackRate = desiredRate', editor_script)
            self.assertIn('operations = draftOperations', editor_script)
            self.assertIn('selectionEnd = sourceToEdited(drag.sourceEnd, false, "right")', editor_script)
            self.assertIn('targetDuration ?? selectionEnd - selectionStart', editor_script)
            self.assertIn('selectionEnd > selectionStart', editor_script)
            self.assertNotIn('selectionEnd - selectionStart >= .01', editor_script)
            self.assertIn('debugLog("play_button"', editor_script)
            self.assertIn('cutSkipTarget = activeCut.end', editor_script)
            self.assertIn('resumePlaybackAfterCut(activeCut.end)', editor_script)
            self.assertIn('async function resumePlaybackAfterCut(sourceTarget)', editor_script)
            self.assertIn('await audio.play();', editor_script)
            self.assertIn('const speed = speedForOperation(existing) * (inside ? relativeRate : 1)', editor_script)
            self.assertIn('cutOperationsForRange(withoutSelectedSilence, sourceStart, sourceEnd)', editor_script)
            self.assertIn('removeInsertedSilenceFromEditedRange(operations, selectionStart, selectionEnd)', editor_script)
            self.assertIn('row.addEventListener("dragstart"', editor_script)
            self.assertIn('mergeApplied = true', editor_script)
            self.assertIn('body.append("editor_state", JSON.stringify(editorState()))', editor_script)
            self.assertIn('function operationsInOrder(', editor_script)
            self.assertIn('trackGroups = [[...trackOrder]]', editor_script)
            self.assertIn('formatRulerTime(time, interval)', editor_script)
            self.assertNotIn('operations.filter(operation => operation.type === "speed").forEach', editor_script)
            with patch("studio.views.start_media_tool_worker", return_value=True) as start:
                response = self.client.post(
                    f"/tools/audio-editor/{job.id}/save/",
                    data=json.dumps({
                        "output_format": "wav",
                        "mp3_bitrate": "320",
                        "trailing_silence": 1,
                        "operations": [{"type": "speed", "start": 1.0, "end": 2.0, "speed": 0.5}],
                    }),
                    content_type="application/json",
                )
            self.assertEqual(response.status_code, 200)
            job.refresh_from_db()
            self.assertEqual(job.options["phase"], "render")
            self.assertEqual(job.options["mp3_bitrate"], "320")
            self.assertEqual(job.options["trailing_silence"], 1)
            self.assertEqual(job.options["operations"][0]["speed"], 0.5)
            start.assert_called_once_with(job)

    def test_audio_editor_persists_applied_merge_across_page_reload(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={},
                processing_status="complete",
                result_manifest={"stage": "editing", "editor_ready": True},
            )
            first = MediaToolInput.objects.create(
                job=job, role="audio", position=0,
                media_file=SimpleUploadedFile("first.wav", self._wav_bytes(.5)),
            )
            second = MediaToolInput.objects.create(
                job=job, role="audio", position=1,
                media_file=SimpleUploadedFile("second.wav", self._wav_bytes(.5)),
            )
            job.result_manifest = {
                "stage": "editing", "editor_ready": True,
                "tracks": [
                    {"input_id": first.id, "duration": .5},
                    {"input_id": second.id, "duration": .5},
                ],
            }
            job.save(update_fields=["result_manifest"])
            state = {
                "trackOrder": [first.id, second.id],
                "trackGroups": [[first.id, second.id]],
                "mergeApplied": True,
                "cursor": .5,
                "selectionStart": None,
                "selectionEnd": None,
                "targetDuration": None,
                "selectionRate": 1,
                "operations": [
                    {"type": "silence", "start": .5, "end": .5, "duration": 1.0},
                ],
                "trailingSilence": 0,
            }

            response = self.client.post(
                f"/tools/audio-editor/{job.id}/draft/",
                data=json.dumps(state),
                content_type="application/json",
            )

            self.assertEqual(response.status_code, 200)
            job.refresh_from_db()
            self.assertTrue(job.options["editor_draft_state"]["mergeApplied"])
            self.assertEqual(job.options["editor_draft_state"]["operations"], state["operations"])
            page = self.client.get(f"/tools/audio-editor/{job.id}/")
            self.assertTrue(page.context["audio_editor_state"]["mergeApplied"])

    def test_audio_editor_preview_supports_byte_ranges_for_seeking(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                processing_status="complete",
                result_manifest={"stage": "editing", "editor_ready": True},
            )
            item = MediaToolInput.objects.create(
                job=job, role="audio", media_file=SimpleUploadedFile("voice.wav", self._wav_bytes(1.0)),
            )
            preview = Path(media_root) / "tools" / "jobs" / str(job.id) / "output" / "preview.mp3"
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(bytes(range(256)) * 4)
            job.result_manifest = {
                "stage": "editing", "editor_ready": True,
                "tracks": [{
                    "input_id": item.id, "name": "voice.wav", "duration": 1.0,
                    "preview": preview.relative_to(media_root).as_posix(), "peaks": "peaks.json",
                }],
            }
            job.save(update_fields=["result_manifest"])

            response = self.client.get(
                f"/tools/audio-editor/{job.id}/preview/{item.id}/",
                HTTP_RANGE="bytes=100-199",
            )

            self.assertEqual(response.status_code, 206)
            self.assertEqual(response["Accept-Ranges"], "bytes")
            self.assertEqual(response["Content-Range"], "bytes 100-199/1024")
            self.assertEqual(response["Content-Length"], "100")
            self.assertEqual(b"".join(response.streaming_content), preview.read_bytes()[100:200])

    def test_audio_editor_result_can_be_committed_as_a_fresh_edit_session(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={"phase": "render", "output_format": "wav", "operations": [{"type": "cut"}]},
                processing_status="complete",
                result_manifest={"stage": "result", "output_format": "wav", "output_duration": 1.0},
            )
            old_input = MediaToolInput.objects.create(
                job=job, role="audio", media_file=SimpleUploadedFile("old.wav", self._wav_bytes(2.0)),
            )
            result = Path(media_root) / "tools" / "jobs" / str(job.id) / "output" / "edited-audio.wav"
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_bytes(self._wav_bytes(1.0))
            job.result_file = result.relative_to(media_root).as_posix()
            job.save(update_fields=["result_file"])

            page = self.client.get(f"/tools/audio-editor/{job.id}/")
            self.assertContains(page, "Продолжить редактирование")
            self.assertContains(page, 'data-duration="1.0"')
            with patch("studio.views.start_media_tool_worker", return_value=True) as start:
                response = self.client.post(f"/tools/audio-editor/{job.id}/continue/")

            self.assertRedirects(response, f"/tools/audio-editor/{job.id}/", fetch_redirect_response=False)
            job.refresh_from_db()
            inputs = list(job.inputs.all())
            self.assertEqual(len(inputs), 1)
            self.assertNotEqual(inputs[0].id, old_input.id)
            self.assertEqual(Path(inputs[0].media_file.path).read_bytes(), self._wav_bytes(1.0))
            self.assertFalse(Path(old_input.media_file.path).exists())
            self.assertEqual(job.options, {
                "output_format": "wav", "mp3_bitrate": "192",
                "phase": "prepare", "editor_track_order": [inputs[0].id],
            })
            self.assertEqual(job.result_manifest, {})
            self.assertEqual(job.result_file, "")
            self.assertFalse(result.exists())
            start.assert_called_once_with(job)

    def test_audio_editor_accepts_duplicate_files_and_renders_them_in_selected_order(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={"output_format": "wav"},
            )
            first = MediaToolInput.objects.create(
                job=job, role="audio", position=0,
                media_file=SimpleUploadedFile("same.wav", self._wav_bytes(1.0)),
            )
            second = MediaToolInput.objects.create(
                job=job, role="audio", position=1,
                media_file=SimpleUploadedFile("same.wav", self._wav_bytes(.6)),
            )
            job.options = {"output_format": "wav", "editor_track_order": [second.id, first.id]}
            job.save(update_fields=["options"])
            process_media_tool_job(job)
            job.refresh_from_db()
            self.assertEqual([item["input_id"] for item in job.result_manifest["tracks"]], [second.id, first.id])
            self.assertEqual([item["name"] for item in job.result_manifest["tracks"]], ["same.wav", "same.wav"])

            job.options = {
                **job.options, "phase": "render", "operations": [],
                "output_format": "wav", "editor_track_order": [second.id, first.id],
            }
            job.save(update_fields=["options"])
            process_media_tool_job(job)
            job.refresh_from_db()
            self.assertEqual(job.result_manifest["stage"], "result")
            self.assertAlmostEqual(job.result_manifest["output_duration"], 1.6, delta=.08)

    def test_audio_editor_can_add_another_track_without_replacing_the_first(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={"output_format": "mp3"},
                processing_status="complete",
                result_manifest={"stage": "editing", "editor_ready": True},
            )
            first = MediaToolInput.objects.create(
                job=job, role="audio", position=0,
                media_file=SimpleUploadedFile("same.wav", self._wav_bytes(.5)),
            )
            job.result_manifest = {
                "stage": "editing", "editor_ready": True,
                "tracks": [{"input_id": first.id, "duration": .5}],
            }
            job.save(update_fields=["result_manifest"])
            with patch("studio.views.start_media_tool_worker", return_value=True) as start:
                response = self.client.post(
                    f"/tools/audio-editor/{job.id}/add/",
                    {
                        "audio": SimpleUploadedFile("same.wav", self._wav_bytes(.5)),
                        "editor_state": json.dumps({
                            "trackOrder": [first.id],
                            "trackGroups": [[first.id]],
                            "mergeApplied": True,
                            "cursor": .2,
                            "selectionStart": .1,
                            "selectionEnd": .4,
                            "targetDuration": .3,
                            "selectionRate": 1,
                            "operations": [
                                {"type": "cut", "start": .1, "end": .2},
                                {"type": "silence", "start": .5, "end": .5, "duration": 1.0},
                            ],
                        }),
                    },
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(job.inputs.filter(role="audio").count(), 2)
            job.refresh_from_db()
            self.assertEqual(job.options["editor_track_order"][0], first.id)
            self.assertEqual(len(job.options["editor_track_order"]), 2)
            draft = job.options["editor_draft_state"]
            self.assertEqual(draft["operations"], [
                {"type": "cut", "start": .1, "end": .2},
                {"type": "silence", "start": .5, "end": .5, "duration": 1.0},
            ])
            self.assertEqual(draft["selectionStart"], .1)
            self.assertEqual(draft["selectionEnd"], .4)
            self.assertEqual(draft["trackGroups"][0], [first.id])
            self.assertEqual(draft["trackGroups"][1], [job.options["editor_track_order"][1]])
            self.assertFalse(draft["mergeApplied"])
            self.assertEqual(job.options["editor_addition_history"][-1]["track_id"], job.options["editor_track_order"][1])
            start.assert_called_once_with(job)

    def test_audio_editor_can_undo_the_last_added_track(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                processing_status="complete",
                result_manifest={"stage": "editing", "editor_ready": True},
            )
            first = MediaToolInput.objects.create(
                job=job, role="audio", position=0,
                media_file=SimpleUploadedFile("first.wav", self._wav_bytes(.5)),
            )
            second = MediaToolInput.objects.create(
                job=job, role="audio", position=1,
                media_file=SimpleUploadedFile("second.wav", self._wav_bytes(.5)),
            )
            state_before = {
                "trackOrder": [first.id], "trackGroups": [[first.id]], "mergeApplied": True,
                "operations": [], "trailingSilence": 1,
            }
            job.options = {
                "editor_track_order": [first.id, second.id],
                "editor_addition_history": [{"track_id": second.id, "state_before": state_before}],
            }
            job.result_manifest = {
                "stage": "editing", "editor_ready": True,
                "tracks": [
                    {"input_id": first.id, "duration": .5},
                    {"input_id": second.id, "duration": .5},
                ],
            }
            job.save(update_fields=["options", "result_manifest"])

            with patch("studio.views.start_media_tool_worker", return_value=True) as start:
                response = self.client.post(
                    f"/tools/audio-editor/{job.id}/remove/",
                    data=json.dumps({"undo_addition": True}),
                    content_type="application/json",
                )

            self.assertEqual(response.status_code, 200)
            job.refresh_from_db()
            self.assertEqual(list(job.inputs.values_list("id", flat=True)), [first.id])
            self.assertEqual(job.options["editor_draft_state"]["trackOrder"], [first.id])
            self.assertEqual(job.options["editor_draft_state"]["trailingSilence"], 1)
            self.assertEqual(job.options["editor_addition_history"], [])
            start.assert_called_once_with(job)

    def test_audio_editor_can_remove_any_track_when_more_than_one_exists(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                processing_status="complete",
                result_manifest={"stage": "editing", "editor_ready": True},
            )
            first = MediaToolInput.objects.create(
                job=job, role="audio", position=0,
                media_file=SimpleUploadedFile("first.wav", self._wav_bytes(.5)),
            )
            second = MediaToolInput.objects.create(
                job=job, role="audio", position=1,
                media_file=SimpleUploadedFile("second.wav", self._wav_bytes(.5)),
            )
            job.result_manifest = {
                "stage": "editing", "editor_ready": True,
                "tracks": [
                    {"input_id": first.id, "duration": .5},
                    {"input_id": second.id, "duration": .5},
                ],
            }
            job.save(update_fields=["result_manifest"])
            remaining_state = {
                "trackOrder": [second.id], "trackGroups": [[second.id]], "mergeApplied": True,
                "operations": [], "trailingSilence": 0,
            }

            with patch("studio.views.start_media_tool_worker", return_value=True) as start:
                response = self.client.post(
                    f"/tools/audio-editor/{job.id}/remove/",
                    data=json.dumps({"track_id": first.id, "editor_state": remaining_state}),
                    content_type="application/json",
                )

            self.assertEqual(response.status_code, 200)
            job.refresh_from_db()
            self.assertEqual(list(job.inputs.values_list("id", flat=True)), [second.id])
            self.assertEqual(job.options["editor_track_order"], [second.id])
            start.assert_called_once_with(job)

    def test_audio_editor_processes_cut_and_speed_without_changing_source(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            source_bytes = self._wav_bytes(4.0)
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={"output_format": "wav"},
            )
            item = MediaToolInput.objects.create(
                job=job, role="audio", media_file=SimpleUploadedFile("voice.wav", source_bytes),
            )
            process_media_tool_job(job)
            job.refresh_from_db()
            self.assertEqual(job.result_manifest["stage"], "editing")
            self.assertTrue((Path(media_root) / job.result_manifest["peaks"]).is_file())
            self.assertEqual(Path(item.media_file.path).read_bytes(), source_bytes)

            job.options = {
                "phase": "render", "output_format": "wav",
                "operations": [
                    {"type": "cut", "start": 1.0, "end": 2.0},
                    {"type": "speed", "start": 2.0, "end": 3.0, "speed": 2.0},
                ],
            }
            job.save(update_fields=["options"])
            process_media_tool_job(job)
            job.refresh_from_db()
            self.assertEqual(job.result_manifest["stage"], "result")
            self.assertAlmostEqual(job.result_manifest["output_duration"], 2.5, delta=.08)
            self.assertEqual(Path(item.media_file.path).read_bytes(), source_bytes)

    def test_audio_editor_replaces_a_tiny_source_interval_with_exact_silence(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={
                    "phase": "render", "output_format": "wav",
                    "operations": [
                        {"type": "silence", "start": .4, "end": .415, "duration": 2.0},
                    ],
                },
            )
            MediaToolInput.objects.create(
                job=job, role="audio", media_file=SimpleUploadedFile("voice.wav", self._wav_bytes(1.0)),
            )

            process_media_tool_job(job)
            job.refresh_from_db()

            self.assertAlmostEqual(job.result_manifest["output_duration"], 2.985, delta=.01)
            self.assertAlmostEqual(probe_duration(Path(media_root) / job.result_file), 2.985, delta=.03)

    def test_audio_editor_can_append_one_second_of_silence(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={
                    "phase": "render", "output_format": "wav",
                    "operations": [], "trailing_silence": 1,
                },
            )
            MediaToolInput.objects.create(
                job=job, role="audio", media_file=SimpleUploadedFile("voice.wav", self._wav_bytes(1.0)),
            )

            process_media_tool_job(job)
            job.refresh_from_db()

            self.assertAlmostEqual(job.result_manifest["output_duration"], 2.0, delta=.01)
            self.assertAlmostEqual(probe_duration(Path(media_root) / job.result_file), 2.0, delta=.03)

    def test_audio_editor_can_insert_silence_between_two_audio_files(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={
                    "phase": "render", "output_format": "wav",
                    "operations": [
                        {"type": "silence", "start": .5, "end": .5, "duration": 1.0},
                    ],
                },
            )
            first = MediaToolInput.objects.create(
                job=job, role="audio", position=0,
                media_file=SimpleUploadedFile("first.wav", self._wav_bytes(.5)),
            )
            second = MediaToolInput.objects.create(
                job=job, role="audio", position=1,
                media_file=SimpleUploadedFile("second.wav", self._wav_bytes(.5)),
            )
            job.options["editor_track_order"] = [first.id, second.id]
            job.result_manifest = {
                "tracks": [
                    {"input_id": first.id, "duration": .5},
                    {"input_id": second.id, "duration": .5},
                ],
            }
            job.save(update_fields=["options", "result_manifest"])

            process_media_tool_job(job)
            job.refresh_from_db()

            self.assertAlmostEqual(job.result_manifest["output_duration"], 2.0, delta=.02)
            self.assertAlmostEqual(probe_duration(Path(media_root) / job.result_file), 2.0, delta=.04)

    def test_audio_editor_mp3_reports_edited_timeline_without_encoder_padding(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={"phase": "render", "output_format": "mp3", "mp3_bitrate": "320", "operations": []},
                result_manifest={"tracks": [{"input_id": 1, "duration": 1.0}]},
            )
            item = MediaToolInput.objects.create(
                job=job, role="audio", media_file=SimpleUploadedFile("voice.wav", self._wav_bytes(1.0)),
            )
            job.result_manifest["tracks"][0]["input_id"] = item.id
            job.options["editor_track_order"] = [item.id]
            job.save(update_fields=["result_manifest", "options"])

            process_media_tool_job(job)
            job.refresh_from_db()

            self.assertEqual(job.result_manifest["output_duration"], 1.0)
            self.assertEqual(job.result_manifest["mp3_bitrate"], "320")
            self.assertGreater(probe_duration(Path(media_root) / job.result_file), 1.0)

    def test_audio_editor_renders_a_second_speed_change_inside_an_already_changed_range(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={
                    "phase": "render", "output_format": "wav",
                    "operations": [
                        {"type": "speed", "start": 0.0, "end": 1.0, "speed": 0.5},
                        {"type": "speed", "start": 1.5, "end": 2.0, "speed": 0.5},
                    ],
                },
            )
            MediaToolInput.objects.create(
                job=job, role="audio", media_file=SimpleUploadedFile("voice.wav", self._wav_bytes(4.0)),
            )
            process_media_tool_job(job)
            job.refresh_from_db()
            # The middle half-second has been returned to 1x inside a formerly 0.5x range.
            self.assertAlmostEqual(job.result_manifest["output_duration"], 5.5, delta=.25)

    def test_audio_editor_rejects_overlapping_operations(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_EDIT_AUDIO,
                options={
                    "phase": "render", "output_format": "mp3",
                    "operations": [
                        {"type": "cut", "start": .5, "end": 1.5},
                        {"type": "speed", "start": 1.0, "end": 2.0, "speed": 1.5},
                    ],
                },
            )
            MediaToolInput.objects.create(
                job=job, role="audio", media_file=SimpleUploadedFile("voice.wav", self._wav_bytes(3.0)),
            )
            with self.assertRaisesRegex(Exception, "must not overlap"):
                process_media_tool_job(job)

    @staticmethod
    def _wav_bytes(duration):
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * int(8000 * duration))
        return output.getvalue()

    def test_phrase_timing_manual_project_opens_reusable_editor(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                "/tools/phrase-timing/",
                {
                    "mode": "manual",
                    "media_file": SimpleUploadedFile("lesson.mp4", b"video", content_type="video/mp4"),
                    "source_text": "Первая фраза. Вторая фраза!",
                },
            )
            project = PhraseTimingProject.objects.get()
            self.assertEqual(response.status_code, 302)
            self.assertIn(str(project.id), response["Location"])
            self.assertEqual([item["text"] for item in project.phrases], ["Первая фраза.", "Вторая фраза!"])
            editor = self.client.get(response["Location"])
            self.assertContains(editor, "phrase-timing-editor")
            self.assertContains(editor, "phrase_timing.js")

    def test_phrase_timing_automatic_mode_creates_background_project_without_text(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with patch("studio.forms.probe_has_audio", return_value=True), \
                 patch("studio.views.start_phrase_timing_worker", return_value=True) as start:
                response = self.client.post(
                    "/tools/phrase-timing/",
                    {
                        "mode": "automatic",
                        "media_file": SimpleUploadedFile("lesson.mp4", b"video", content_type="video/mp4"),
                        "source_text": "",
                    },
                )
            project = PhraseTimingProject.objects.get()
            self.assertEqual(response.status_code, 302)
            self.assertEqual(project.processing_status, "pending")
            start.assert_called_once_with(project)

    def test_automatic_timing_renders_source_text_field_hidden(self):
        form = PhraseTimingToolForm(initial={"mode": PhraseTimingProject.MODE_AUTOMATIC})
        response = self.client.get("/tools/phrase-timing/")
        rendered = Template(
            '{% load i18n %}{% include "studio/includes/phrase_timing_setup_fields.html" with form=form %}'
        ).render(Context({"form": form}))
        self.assertIn("data-timing-source-field hidden", rendered)
        self.assertContains(response, "data-timing-source-field")

    def test_automatic_timing_rejects_video_without_audio(self):
        with patch("studio.forms.probe_has_audio", return_value=False):
            response = self.client.post(
                "/tools/phrase-timing/",
                {
                    "mode": "automatic",
                    "media_file": SimpleUploadedFile("silent.mp4", b"video", content_type="video/mp4"),
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "нет аудиодорожки")
        self.assertFalse(PhraseTimingProject.objects.exists())

    def test_phrase_timing_start_has_mode_specific_guides(self):
        response = self.client.get("/tools/phrase-timing/")
        self.assertContains(response, 'data-timing-guide="manual"')
        self.assertContains(response, 'data-timing-guide="automatic" hidden')
        self.assertContains(response, "Перейти к ручной разметке")
        self.assertContains(response, "Распознать речь и создать таймкоды")
        self.assertContains(response, "Если озвучки ещё нет в файле")
        self.assertContains(response, "Если в файле уже есть нужная речь")
        self.assertContains(response, "Когда озвучку нужно расположить вручную")
        self.assertContains(response, "Когда в файле уже звучит нужный текст")
        self.assertContains(response, "DOMContentLoaded")
        self.assertContains(response, "document.querySelectorAll('[data-timing-guide]')")

    def test_quick_review_is_only_available_for_automatic_timing(self):
        manual = PhraseTimingProject.objects.create(
            video_file="tools/phrase-timing/test/input/manual.mp4",
            mode=PhraseTimingProject.MODE_MANUAL,
            processing_status="complete",
            phrases=[{"id": 1, "text": "Фраза", "start": 0.0, "end": 1.0}],
        )
        automatic = PhraseTimingProject.objects.create(
            video_file="tools/phrase-timing/test/input/automatic.mp4",
            mode=PhraseTimingProject.MODE_AUTOMATIC,
            processing_status="complete",
            phrases=[{"id": 1, "text": "Фраза", "start": 0.0, "end": 1.0}],
        )

        manual_response = self.client.get(f"/tools/phrase-timing/{manual.id}/")
        automatic_response = self.client.get(f"/tools/phrase-timing/{automatic.id}/")

        self.assertNotContains(manual_response, 'data-timing-review')
        self.assertNotContains(manual_response, 'id="timing-review-controls"')
        self.assertContains(automatic_response, 'data-timing-review')
        self.assertContains(automatic_response, 'id="timing-review-controls"')

    def test_automatic_phrase_timing_worker_saves_phrases_and_vad_review(self):
        payload = {
            "words": [{"word": "Готово.", "start": 0.2, "end": 0.8}],
            "segments": [],
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            project = PhraseTimingProject.objects.create(
                video_file=SimpleUploadedFile("lesson.mp4", b"video", content_type="video/mp4"),
                mode=PhraseTimingProject.MODE_AUTOMATIC,
                processing_status="queued",
            )
            with patch("studio.services.phrase_timing.probe_has_audio", return_value=True), \
                 patch("studio.services.phrase_timing.run"), \
                 patch("studio.services.phrase_timing._transcribe_long", return_value=payload), \
                 patch("studio.services.phrase_timing.speech_probabilities", return_value=([.9] * 80, 80 * 512)), \
                 patch("studio.services.phrase_timing.probe_duration", return_value=2.0):
                process_phrase_timing_project(project)
            project.refresh_from_db()
            self.assertEqual(project.processing_status, "complete")
            self.assertEqual(project.phrases[0]["text"], "Готово.")
            self.assertTrue(project.phrases[0]["words"])
            self.assertTrue(project.review_items)
            self.assertEqual(project.review_items[0]["kind"], "unrecognized_speech")

    def test_phrase_timing_status_reports_background_progress(self):
        project = PhraseTimingProject.objects.create(
            video_file="tools/phrase-timing/test/input/video.mp4",
            mode=PhraseTimingProject.MODE_AUTOMATIC,
            processing_status="processing",
            processing_step="checking_voice",
        )
        response = self.client.get(f"/tools/phrase-timing/{project.id}/status/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["step"], "checking_voice")

    def test_automatic_editor_does_not_present_a_next_phrase_state(self):
        project = PhraseTimingProject.objects.create(
            video_file="tools/phrase-timing/test/input/video.mp4",
            mode=PhraseTimingProject.MODE_AUTOMATIC,
            processing_status="complete",
            phrases=[{"id": 1, "text": "Фраза", "start": 0.0, "end": 1.0, "end_explicit": True}],
        )
        response = self.client.get(f"/tools/phrase-timing/{project.id}/")
        self.assertContains(response, 'data-project-mode="automatic"')
        self.assertContains(response, 'data-label-edit-phrase="Редактировать фразу и таймкоды"')
        self.assertContains(response, 'id="timing-phrase-edit-dialog"')
        self.assertContains(response, 'id="timing-overlap-warning"')
        self.assertNotContains(response, "Следующая фраза")

    def test_phrase_timing_video_supports_byte_ranges(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            project = PhraseTimingProject.objects.create(
                video_file=SimpleUploadedFile("lesson.mp4", b"0123456789", content_type="video/mp4"),
                source_text="Фраза",
                phrases=[{"id": 1, "text": "Фраза", "start": None, "end": None}],
            )
            response = self.client.get(
                f"/tools/phrase-timing/{project.id}/video/",
                HTTP_RANGE="bytes=3-6",
            )
            content = b"".join(response.streaming_content)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 3-6/10")
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertEqual(content, b"3456")

    def test_phrase_timing_accepts_audio_and_uses_the_same_editor(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                "/tools/phrase-timing/",
                {
                    "mode": "manual",
                    "media_file": SimpleUploadedFile("voice.wav", b"audio", content_type="audio/wav"),
                    "source_text": "Первая фраза.",
                },
            )
            project = PhraseTimingProject.objects.get()
            editor = self.client.get(response["Location"])
            media = self.client.get(f"/tools/phrase-timing/{project.id}/media/")
        self.assertEqual(response.status_code, 302)
        self.assertContains(editor, "<audio", html=False)
        self.assertEqual(media["Content-Type"], "audio/x-wav")

    def test_phrase_timing_state_and_exports_are_saved(self):
        project = PhraseTimingProject.objects.create(
            video_file="tools/phrase-timing/test/input/video.mp4",
            source_text="Первая\nВторая",
            phrases=[
                {"id": 1, "text": "Первая", "start": None, "end": None},
                {"id": 2, "text": "Вторая", "start": None, "end": None},
            ],
        )
        response = self.client.post(
            f"/tools/phrase-timing/{project.id}/save/",
            data=json.dumps({
                "duration": 8,
                "phrases": [
                    {"text": "Первая", "start": 1.25},
                    {"text": "Вторая", "start": 3.5},
                ],
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.phrases[0]["end"], 3.5)
        exported = self.client.get(f"/tools/phrase-timing/{project.id}/export/srt/")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("00:00:01,250 --> 00:00:03,500", exported.content.decode())

    def test_phrase_timing_export_rejects_an_unmarked_first_phrase(self):
        project = PhraseTimingProject.objects.create(
            video_file="tools/phrase-timing/test/input/video.mp4",
            source_text="Первая\nВторая",
            phrases=[
                {"id": 1, "text": "Первая", "start": None, "end": None},
                {"id": 2, "text": "Вторая", "start": 3.5, "end": 8.0},
            ],
            video_duration=8,
        )
        response = self.client.get(
            f"/tools/phrase-timing/{project.id}/export/srt/?include_unmarked=1",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("first phrase", response.content.decode())

    def test_phrase_timing_text_and_timecodes_can_be_edited_together(self):
        project = PhraseTimingProject.objects.create(
            video_file="tools/phrase-timing/test/input/video.mp4",
            source_text="Первая\nВторая",
            phrases=[
                {"id": 1, "text": "Первая", "start": 1.0, "end": 3.0},
                {"id": 2, "text": "Вторая", "start": 3.0, "end": 5.0},
            ],
            video_duration=8,
        )
        response = self.client.post(
            f"/tools/phrase-timing/{project.id}/resegment/",
            data=json.dumps({"text": "[00:01.500] Первая исправлена\nВторая", "duration": 8}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.phrases[0]["start"], 1.5)
        self.assertIsNone(project.phrases[1]["start"])
        self.assertEqual(project.source_text, "Первая исправлена\nВторая")

    def test_audio_to_text_interface_is_available(self):
        response = self.client.get("/tools/audio-to-text/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Точная расшифровка")
        self.assertContains(response, "Читаемый текст")
        self.assertContains(response, "Имена и специальные термины")
        self.assertNotContains(response, "прототип")

    def test_audio_to_text_upload_creates_background_job(self):
        with patch("studio.views.start_media_tool_worker", return_value=True) as start:
            response = self.client.post(
                "/tools/audio-to-text/",
                {
                    "media": SimpleUploadedFile("speech.wav", b"audio", content_type="audio/wav"),
                    "language": "ru",
                    "export_format": "both",
                    "transcription_mode": "readable",
                    "terms": "FormaCode, Pulse",
                    "divide_paragraphs": "on",
                    "preserve_wording": "on",
                },
            )
        job = MediaToolJob.objects.get(kind=MediaToolJob.KIND_TRANSCRIBE)
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(job.id), response["Location"])
        self.assertEqual(job.options["language"], "ru")
        self.assertEqual(job.inputs.get().role, "media")
        start.assert_called_once_with(job)

    def test_audio_to_text_accepts_long_original_filename(self):
        long_name = f"{'long-video-name-' * 11}.mp4"
        self.assertGreater(len(long_name), 100)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                with patch("studio.views.start_media_tool_worker", return_value=True):
                    response = self.client.post(
                        "/tools/audio-to-text/",
                        {
                            "media": SimpleUploadedFile(long_name, b"video", content_type="video/mp4"),
                            "language": "ru",
                            "export_format": "both",
                            "transcription_mode": "readable",
                            "preserve_wording": "on",
                        },
                    )
                self.assertEqual(response.status_code, 302)
                media_input = MediaToolInput.objects.get(job__kind=MediaToolJob.KIND_TRANSCRIBE)
                self.assertGreater(len(media_input.media_file.name), 100)
                self.assertTrue(Path(media_input.media_file.path).is_file())

    def test_audio_to_text_job_produces_editable_text_and_valid_docx(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                job = MediaToolJob.objects.create(
                    kind=MediaToolJob.KIND_TRANSCRIBE,
                    options={
                        "language": "en", "export_format": "both",
                        "transcription_mode": "readable", "divide_paragraphs": True,
                        "preserve_wording": True,
                    },
                )
                MediaToolInput.objects.create(
                    job=job, role="media", position=0,
                    media_file=SimpleUploadedFile("speech.wav", b"audio", content_type="audio/wav"),
                )
                payload = {
                    "text": "hello world second paragraph",
                    "language": "en", "duration": 4.0,
                    "words": [{"word": "hello", "start": 0, "end": .4}],
                    "segments": [
                        {"text": "hello world", "start": 0, "end": 1},
                        {"text": "second paragraph", "start": 2.6, "end": 4},
                    ],
                }
                with patch("studio.services.tool_processing.run"), patch(
                    "studio.services.tool_processing._transcribe_prepared_audio", return_value=payload,
                ), patch(
                    "studio.services.tool_processing._format_readable_text",
                    return_value="Hello, world!\n\nSecond paragraph.",
                ):
                    process_media_tool_job(job)
                job.refresh_from_db()
                self.assertEqual(job.processing_status, "complete")
                self.assertEqual(job.result_manifest["text"], "Hello, world!\n\nSecond paragraph.")
                self.assertTrue((Path(media_root) / job.result_file).is_file())
                import zipfile
                with zipfile.ZipFile(Path(media_root) / job.result_manifest["docx"]) as archive:
                    self.assertIn("word/document.xml", archive.namelist())

    def test_transcription_download_uses_edited_text(self):
        job = MediaToolJob.objects.create(
            kind=MediaToolJob.KIND_TRANSCRIBE,
            processing_status="complete",
            result_manifest={"text": "Original"},
        )
        response = self.client.post(
            f"/tools/audio-to-text/{job.id}/download/txt/",
            {"text": "Edited transcript"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "Edited transcript\n")
        self.assertIn("transcript.txt", response["Content-Disposition"])

    def test_docx_builder_escapes_user_text(self):
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(build_docx_bytes("A & B < C"))) as archive:
            document = archive.read("word/document.xml").decode()
        self.assertIn("A &amp; B &lt; C", document)

    def test_home_presents_three_equal_workflow_cards(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="workflow-choice"', count=3)
        self.assertNotContains(response, 'class="scenario-card"')
        self.assertNotContains(response, "Interface preview")
        self.assertNotContains(response, "coming-soon")
        self.assertContains(response, "Сценарии")

    def test_language_switcher_changes_language_and_keeps_current_page(self):
        response = self.client.post(
            "/i18n/setlang/",
            {"language": "en", "next": "/tools/"},
        )
        self.assertRedirects(response, "/tools/", fetch_redirect_response=False)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "en")

        response = self.client.get("/tools/")
        self.assertContains(response, 'value="en" title="English" aria-pressed="true" class="active"')
        self.assertContains(response, ">Tools<")

        response = self.client.post(
            "/i18n/setlang/",
            {"language": "ru", "next": "/tools/"},
        )
        self.assertRedirects(response, "/tools/", fetch_redirect_response=False)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "ru")

        response = self.client.get("/tools/")
        self.assertContains(response, 'value="ru" title="Русский" aria-pressed="true" class="active"')
        self.assertContains(response, ">Инструменты<")

    def test_each_media_tool_has_a_working_form(self):
        for slug in (
            "replace-fragment",
            "split-phrases",
            "fit-subtitles",
            "prepare-voiceover",
            "check-durations",
            "create-subtitles",
        ):
            response = self.client.get(f"/tools/prototype/{slug}/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "prototype-form")
            self.assertContains(response, 'type="submit"')
            self.assertNotContains(response, "processing is not connected")

    def test_split_tool_accepts_text_with_one_phrase_per_non_empty_line(self):
        with patch("studio.views.start_media_tool_worker", return_value=True):
            response = self.client.post(
                "/tools/prototype/split-phrases/",
                {
                    "audio": SimpleUploadedFile("voice.wav", b"audio", content_type="audio/wav"),
                    "script_text": "  Первая фраза.  \n\n   \nВторая фраза.\n",
                    "output_format": "wav",
                },
            )
        self.assertEqual(response.status_code, 302)
        job = MediaToolJob.objects.get()
        self.assertEqual(job.kind, MediaToolJob.KIND_SPLIT)
        self.assertEqual(job.options["script_text"], "Первая фраза.  \n\n   \nВторая фраза.")
        self.assertEqual(job.options["mp3_bitrate"], "192")
        self.assertEqual(job.inputs.get().role, "audio")

    def test_split_tool_explains_phrase_breakdown_and_reuses_boundary_editor(self):
        page = self.client.get("/tools/prototype/split-phrases/")
        self.assertContains(page, 'id="split-phrase-preview"')
        self.assertContains(page, 'data-phrase-label="Фраза"')
        self.assertContains(page, "Каждая непустая строка станет отдельной фразой")
        self.assertContains(page, 'id="id_mp3_bitrate"')
        self.assertContains(page, "320 кбит/с — максимальное качество")
        self.assertContains(page, 'id="split-audio-duration"')
        self.assertContains(page, 'data-label="Длительность"')
        script = (Path(settings.BASE_DIR) / "studio/static/studio/split_phrases_form.js").read_text()
        quality_script = (Path(settings.BASE_DIR) / "studio/static/studio/mp3_quality.js").read_text()
        self.assertIn('.split(/\\r?\\n/)', script)
        self.assertIn('.filter(Boolean)', script)
        self.assertIn('format.value === "mp3"', quality_script)
        self.assertIn('URL.createObjectURL(file)', script)
        self.assertIn('Math.round(value * 1000)', script)

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            source = Path(media_root) / "tools" / "source.wav"
            clip = Path(media_root) / "tools" / "phrase.wav"
            peaks = Path(media_root) / "tools" / "peaks.json"
            for path, content in ((source, self._wav_bytes(2)), (clip, self._wav_bytes(1)), (peaks, b"[]")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_SPLIT,
                processing_status="complete",
                result_file="tools/phrases.zip",
                result_manifest={
                    "source": "tools/source.wav", "peaks": "tools/peaks.json", "duration": 2,
                    "segments": [{
                        "position": 1, "text": "Первая фраза.", "audio_start": 0,
                        "audio_end": 1, "score": 1, "status": "matched", "manual": False,
                        "duration": 1, "file": "tools/phrase.wav",
                    }],
                },
            )
            result = self.client.get(f"/tools/job/{job.id}/")
            self.assertContains(result, "Скачать все фразы в ZIP")
            self.assertContains(result, "Редактировать границы вручную")
            self.assertContains(result, 'id="segment-editor"')
            self.assertContains(result, 'class="segment-player-seek"')
            self.assertContains(result, f"/tools/split-phrases/{job.id}/phrase/1/editor/")

            ranged = self.client.get(
                f"/tools/split-phrases/{job.id}/phrase/1/",
                HTTP_RANGE="bytes=100-199",
            )
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged["Accept-Ranges"], "bytes")
            self.assertEqual(ranged["Content-Range"], f"bytes 100-199/{clip.stat().st_size}")
            self.assertEqual(ranged["Content-Length"], "100")

            saved = self.client.post(
                f"/tools/split-phrases/{job.id}/phrase/1/boundary/",
                data=json.dumps({"audio_start": .25, "audio_end": 1.5}),
                content_type="application/json",
            )
            self.assertEqual(saved.status_code, 200)
            job.refresh_from_db()
            updated = job.result_manifest["segments"][0]
            self.assertTrue(updated["manual"])
            self.assertEqual(updated["audio_start"], .25)
            self.assertEqual(updated["audio_end"], 1.5)
            self.assertAlmostEqual(updated["duration"], 1.25, delta=.04)
            self.assertTrue((Path(media_root) / job.result_file).is_file())

    def test_split_tool_recognizes_aligns_trims_and_cuts_each_phrase(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_SPLIT,
                options={
                    "script_text": "Первая фраза.\n\nВторая фраза.\n   ",
                    "output_format": "mp3", "mp3_bitrate": "320",
                },
            )
            MediaToolInput.objects.create(
                job=job, role="audio",
                media_file=SimpleUploadedFile("voice.wav", self._wav_bytes(3), content_type="audio/wav"),
            )

            def aligned(entries, words):
                for index, entry in enumerate(entries):
                    entry.update(
                        audio_start=index * 1.25,
                        audio_end=index * 1.25 + 1,
                        score=.98,
                        status="matched",
                    )
                return entries

            with patch("studio.services.tool_processing.transcribe_long_audio", return_value={"words": []}), \
                 patch("studio.services.tool_processing.align_entries", side_effect=aligned), \
                 patch("studio.services.tool_processing.snap_boundaries", side_effect=lambda entries, silences: entries), \
                 patch("studio.services.tool_processing.detect_silences", return_value=[]):
                process_media_tool_job(job)

            job.refresh_from_db()
            self.assertEqual(job.processing_status, "complete")
            self.assertEqual(job.result_manifest["mp3_bitrate"], "320")
            self.assertEqual([item["text"] for item in job.result_manifest["segments"]], [
                "Первая фраза.", "Вторая фраза.",
            ])
            self.assertTrue(all(item["file"].endswith(".mp3") for item in job.result_manifest["segments"]))
            self.assertTrue(all((Path(media_root) / item["file"]).is_file() for item in job.result_manifest["segments"]))
            archive = Path(media_root) / job.result_file
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(len(bundle.namelist()), 2)

    def test_media_tool_upload_creates_background_job(self):
        with patch("studio.views.start_media_tool_worker", return_value=True) as start:
            response = self.client.post(
                "/tools/prototype/prepare-voiceover/",
                {
                    "files": [SimpleUploadedFile("voice.wav", b"audio", content_type="audio/wav")],
                    "trim_edges": "on", "normalize": "on", "prevent_clipping": "on",
                    "soft_edges": "on", "output_format": "wav", "loudness_target": "-16",
                },
            )
        job = MediaToolJob.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(job.kind, MediaToolJob.KIND_PREPARE)
        self.assertEqual(job.inputs.get().role, "voiceover")
        start.assert_called_once_with(job)

    def test_prepare_voiceover_uses_the_shared_mp3_quality(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            job = MediaToolJob.objects.create(
                kind=MediaToolJob.KIND_PREPARE,
                options={
                    "output_format": "mp3", "mp3_bitrate": "256",
                    "trim_edges": False, "normalize": False,
                    "prevent_clipping": False, "soft_edges": False,
                },
            )
            MediaToolInput.objects.create(
                job=job, role="voiceover",
                media_file=SimpleUploadedFile("voice.wav", self._wav_bytes(1.0)),
            )

            process_media_tool_job(job)
            job.refresh_from_db()

            self.assertEqual(job.processing_status, "complete")
            self.assertEqual(job.result_manifest["mp3_bitrate"], "256")
            self.assertTrue(job.result_file.endswith(".mp3"))
            self.assertTrue((Path(media_root) / job.result_file).is_file())

    def test_valid_upload_creates_job_without_waiting_for_ffmpeg(self):
        video = SimpleUploadedFile(
            "source.mp4",
            b"video",
            content_type="video/mp4",
        )
        audio = SimpleUploadedFile(
            "voice.mp3",
            b"audio",
            content_type="audio/mpeg",
        )
        with patch(
            "studio.views.start_merge_worker",
            return_value=True,
        ) as start:
            response = self.client.post(
                "/tools/merge-video-audio/",
                {
                    "video_file": video,
                    "audio_file": audio,
                    "mix_original": "on",
                },
            )
        job = VideoAudioMergeJob.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(job.id), response["Location"])
        self.assertTrue(job.mix_original)
        start.assert_called_once()

    def test_ffmpeg_filter_matches_selected_mode(self):
        video_info = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "10.0"},
        }
        audio_info = {
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
            "format": {"duration": "4.0"},
        }
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                mixed = VideoAudioMergeJob.objects.create(
                    video_file="mixed-video.mp4",
                    audio_file="mixed-audio.m4a",
                    mix_original=True,
                )
                with patch(
                    "studio.services.media_tools.probe_media",
                    side_effect=[video_info, audio_info],
                ), patch(
                    "studio.services.media_tools.run"
                ) as run_mixed:
                    merge_video_with_audio(mixed)
                mixed_command = run_mixed.call_args.args[0]
                mixed_filter = mixed_command[
                    mixed_command.index("-filter_complex") + 1
                ]
                self.assertIn("amix=inputs=2", mixed_filter)

                replaced = VideoAudioMergeJob.objects.create(
                    video_file="replaced-video.mp4",
                    audio_file="replaced-audio.m4a",
                    mix_original=False,
                )
                with patch(
                    "studio.services.media_tools.probe_media",
                    side_effect=[video_info, audio_info],
                ), patch(
                    "studio.services.media_tools.run"
                ) as run_replaced:
                    merge_video_with_audio(replaced)
                replaced_command = run_replaced.call_args.args[0]
                replaced_filter = replaced_command[
                    replaced_command.index("-filter_complex") + 1
                ]
                self.assertNotIn("amix", replaced_filter)
                self.assertIn("[1:a:0]", replaced_filter)


class AccountAndDashboardTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="pavel", password="test-password"
        )
        self.other = get_user_model().objects.create_user(
            username="other", password="test-password"
        )
        self.client = Client(HTTP_HOST="localhost", HTTP_ACCEPT_LANGUAGE="ru")
        self.client.force_login(self.user)

    def test_dashboard_contains_only_owned_projects(self):
        own = Project.objects.create(
            owner=self.user,
            audio_file="jobs/own/input/audio/pt.webm",
            timecodes_text="00:00 - Test",
            video_file="jobs/own/input/video/video.mp4",
        )
        foreign = Project.objects.create(
            owner=self.other,
            audio_file="jobs/foreign/input/audio/en.webm",
            timecodes_text="00:00 - Test",
            video_file="jobs/foreign/input/video/video.mp4",
        )
        response = self.client.get("/projects/")
        self.assertContains(response, own.audio_name)
        self.assertNotContains(response, foreign.audio_name)
        self.assertEqual(
            self.client.get(f"/project/{foreign.id}/").status_code,
            404,
        )
        self.assertEqual(response.content.decode().count('/start/'), 1)

    def test_empty_dashboard_has_at_most_two_project_creation_links(self):
        response = self.client.get("/projects/")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(response.content.decode().count('/start/'), 2)

    def test_new_project_opens_workflow_selector_and_prototype_pages(self):
        selector = self.client.get("/start/")
        localization = self.client.get("/start/localization/")
        takes = self.client.get("/start/takes/")
        self.assertContains(selector, "Выберите сценарий работы")
        self.assertContains(selector, "/start/voiceover/")
        self.assertContains(selector, "/start/localization/")
        self.assertContains(selector, "/start/takes/")
        self.assertContains(localization, "prototype-form")
        self.assertContains(takes, "prototype-form")

    def test_localization_project_creates_language_versions(self):
        response = self.client.post(
            "/start/localization/",
            {
                "title": "Campaign",
                "source_video": SimpleUploadedFile("source.mp4", b"video", content_type="video/mp4"),
                "source_script_text": "00:00 - First\n00:03 - Second",
                "languages": ["en", "de"],
                "copy_settings": "on",
            },
        )
        localization = LocalizationProject.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(localization.versions.values_list("language_code", flat=True)), {"en", "de"})
        self.assertIn(str(localization.id), response["Location"])

    def test_takes_project_accepts_one_long_recording(self):
        with patch("studio.views.start_takes_worker", return_value=True) as start:
            response = self.client.post(
                "/start/takes/",
                {
                    "title": "Spot",
                    "video_file": SimpleUploadedFile("spot.mp4", b"video", content_type="video/mp4"),
                    "script_text": "00:00 - First\n00:03 - Second",
                    "mode": "lines",
                    "recordings": [SimpleUploadedFile("session.wav", b"audio", content_type="audio/wav")],
                },
            )
        project = TakesProject.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(project.recordings.count(), 1)
        start.assert_called_once_with(project)

    def test_selecting_take_deselects_previous_candidate(self):
        project = TakesProject.objects.create(
            owner=self.user, video_file="takes/test/source/video.mp4",
            script_text="00:00 - Test",
        )
        recording = TakeRecording.objects.create(takes_project=project, position=1, media_file="takes/test/recording.wav")
        phrase = TakePhrase.objects.create(takes_project=project, position=1, text="Test", video_start=0, video_end=3)
        first = TakeCandidate.objects.create(phrase=phrase, recording=recording, source_start=0, source_end=1, selected=True)
        second = TakeCandidate.objects.create(phrase=phrase, recording=recording, source_start=2, source_end=3)
        response = self.client.post(f"/takes/{project.id}/candidate/{second.id}/select/")
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(first.selected)
        self.assertTrue(second.selected)

    def test_delete_project_removes_database_row_and_job_directory(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                project = Project.objects.create(
                    owner=self.user,
                    audio_file="jobs/delete/input/audio/test.webm",
                    timecodes_text="00:00 - Test",
                    video_file="jobs/delete/input/video/video.mp4",
                    processing_status="complete",
                )
                job_directory = Path(media_root) / "jobs" / str(project.id)
                job_directory.mkdir(parents=True)
                (job_directory / "large-output.mp4").write_bytes(b"result")

                response = self.client.post(f"/project/{project.id}/delete/")

                self.assertEqual(response.status_code, 302)
                self.assertFalse(Project.objects.filter(pk=project.id).exists())
                self.assertFalse(job_directory.exists())
