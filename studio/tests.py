import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import translation

from .models import ManualSegmentBoundary, Project, Segment, SegmentReplacement, VideoAudioMergeJob
from .services.media_tools import merge_video_with_audio
from .services.voiceover import align_entries, parse_timecodes, snap_boundaries


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
            audio_html = self.client.get(
                f"/project/{self.project.id}/audio/"
            ).content.decode()
            video_html = self.client.get(
                f"/project/{self.project.id}/video/"
            ).content.decode()
            self.assertIn("Назад", audio_html)
            self.assertIn("Вперёд", audio_html)
            self.assertIn("Назад", video_html)
            self.assertIn("Вперёд", video_html)

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
        worker.assert_called_once_with(self.project, clear_manual_boundaries=True)


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
        self.assertIn("--reuse-transcription", spawn.call_args.args[0])
        self.assertEqual(spawn.call_args.args[0][-2:], ["--realign-from", "7"])

    def test_full_processing_clears_manual_boundaries_before_spawn(self):
        from .workers import start_project_worker

        ManualSegmentBoundary.objects.create(
            project=self.project, position=1, audio_start=1.0, audio_end=2.0
        )
        with patch("studio.workers._spawn", return_value=123) as spawn:
            self.assertTrue(start_project_worker(self.project, clear_manual_boundaries=True))

        self.assertFalse(self.project.manual_boundaries.exists())
        self.assertNotIn("--reuse-transcription", spawn.call_args.args[0])

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
        self.assertContains(merge, "Наложить поверх исходного звука")
        self.assertContains(merge, 'class="toggle"')

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
