import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from batch.anki_notes import AnkiNotesBatch
from helpers.logger import log
from services.anki_connector import AnkiConnector


class TestAnkiConnectorNotes(unittest.TestCase):
    def test_all_notes_uses_chunked_notes_info(self):
        connector = AnkiConnector()
        connector.call = MagicMock(side_effect=[[1, 2, 3], [{"noteId": 1}, {"noteId": 2}], [{"noteId": 3}]])

        notes = connector.all_notes(chunk_size=2)

        self.assertEqual([note["noteId"] for note in notes], [1, 2, 3])
        self.assertEqual(connector.call.call_args_list[0].args[:2], ("findNotes", {"query": ""}))
        self.assertEqual(connector.call.call_args_list[1].args[:2], ("notesInfo", {"notes": [1, 2]}))

    def test_all_notes_applies_limit_before_fetching_fields(self):
        connector = AnkiConnector()
        connector.call = MagicMock(side_effect=[[1, 2, 3], [{"noteId": 1}, {"noteId": 2}]])

        notes = connector.all_notes(limit=2)

        self.assertEqual([note["noteId"] for note in notes], [1, 2])
        connector.call.assert_called_with("notesInfo", {"notes": [1, 2]})


class TestAnkiNotesBatch(unittest.TestCase):
    def test_skip_images_skips_generation_but_processes_other_fields(self):
        note = {
            "noteId": 15,
            "modelName": "WordModel",
            "fields": {
                "Word": {"value": "未知"},
                "Reading": {"value": ""},
                "ImagePrompt": {"value": "draw this"},
                "Image": {"value": ""},
                "Reading_Audio": {"value": ""},
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.anki_notes.AnkiConnector") as connector_cls,
            patch("batch.anki_notes.JobManager") as jobs_cls,
            patch("batch.anki_notes.WordGenerator") as word_cls,
            patch("batch.anki_notes.ImageGenerator") as image_cls,
            patch("batch.anki_notes.TTSGenerator") as tts_cls,
            patch("batch.anki_notes.log"),
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = [note]
            jobs_cls.return_value.try_start_anki_note_job.return_value = 103
            word_cls.return_value.generate_missing_fields.side_effect = (
                lambda _row, fields, _model_fields: fields.update(Reading="みち")
            )
            tts_cls.return_value.generate_audio_fields.return_value = None
            batch = AnkiNotesBatch(
                {"anki_import": {"note_types": {"word": "WordModel"}}},
                Path(tmp) / "config.json",
            )

            result = batch.process(skip_images=True)

        image_cls.return_value.generate_for_note.assert_not_called()
        self.assertEqual((result.processed, result.failed), (1, 0))
        connector.update_note_fields.assert_called_once()
        self.assertEqual(connector.update_note_fields.call_args.args[1], {"Reading": "みち"})

    def test_process_uses_runtime_limit(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.anki_notes.AnkiConnector") as connector_cls,
            patch("batch.anki_notes.JobManager"),
            patch("batch.anki_notes.WordGenerator"),
            patch("batch.anki_notes.ImageGenerator"),
            patch("batch.anki_notes.TTSGenerator"),
            patch("batch.anki_notes.log"),
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = []
            batch = AnkiNotesBatch({}, Path(tmp) / "config.json")

            result = batch.process(limit=5)

        self.assertEqual(result.processed, 0)
        connector.all_notes.assert_called_once_with()

    def test_process_uses_configured_limit_when_runtime_limit_omitted(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.anki_notes.AnkiConnector") as connector_cls,
            patch("batch.anki_notes.JobManager"),
            patch("batch.anki_notes.WordGenerator"),
            patch("batch.anki_notes.ImageGenerator"),
            patch("batch.anki_notes.TTSGenerator"),
            patch("batch.anki_notes.log"),
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = []
            batch = AnkiNotesBatch(
                {"services": {"anki_notes": {"limit": 3}}},
                Path(tmp) / "config.json",
            )

            batch.process()

        connector.all_notes.assert_called_once_with()

    def test_sync_is_disabled_by_default(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.anki_notes.AnkiConnector") as connector_cls,
            patch("batch.anki_notes.JobManager"),
            patch("batch.anki_notes.WordGenerator"),
            patch("batch.anki_notes.ImageGenerator"),
            patch("batch.anki_notes.TTSGenerator"),
            patch("batch.anki_notes.log"),
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = []
            batch = AnkiNotesBatch({}, Path(tmp) / "config.json")

            batch.process()

        connector.sync.assert_not_called()

    def test_sync_before_and_after_can_be_enabled_independently(self):
        for settings, expected_calls in (
            ({"sync_before_processing": True}, 1),
            ({"sync_after_processing": True}, 1),
            (
                {
                    "sync_before_processing": True,
                    "sync_after_processing": True,
                },
                2,
            ),
        ):
            with (
                tempfile.TemporaryDirectory() as tmp,
                patch("batch.anki_notes.AnkiConnector") as connector_cls,
                patch("batch.anki_notes.JobManager"),
                patch("batch.anki_notes.WordGenerator"),
                patch("batch.anki_notes.ImageGenerator"),
                patch("batch.anki_notes.TTSGenerator"),
                patch("batch.anki_notes.log"),
            ):
                connector = connector_cls.return_value
                connector.all_notes.return_value = []
                batch = AnkiNotesBatch(
                    {"services": {"anki_notes": settings}},
                    Path(tmp) / "config.json",
                )

                batch.process()

            self.assertEqual(connector.sync.call_count, expected_calls)

    def test_limit_counts_only_notes_with_field_changes(self):
        complete_note = {
            "noteId": 20,
            "modelName": "WordModel",
            "fields": {"Word": {"value": "既知"}, "Reading": {"value": "きち"}},
        }
        incomplete_note = {
            "noteId": 21,
            "modelName": "WordModel",
            "fields": {"Word": {"value": "未知"}, "Reading": {"value": ""}},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.anki_notes.AnkiConnector") as connector_cls,
            patch("batch.anki_notes.JobManager") as jobs_cls,
            patch("batch.anki_notes.WordGenerator") as word_cls,
            patch("batch.anki_notes.ImageGenerator"),
            patch("batch.anki_notes.TTSGenerator"),
            patch("batch.anki_notes.log"),
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = [complete_note, incomplete_note]

            def fill_missing(row, fields, _model_fields):
                if row["Word"] == "未知":
                    fields["Reading"] = "みち"

            word_cls.return_value.generate_missing_fields.side_effect = fill_missing
            batch = AnkiNotesBatch(
                {"anki_import": {"note_types": {"word": "WordModel"}}},
                Path(tmp) / "config.json",
            )

            result = batch.process(limit=1)

        self.assertEqual((result.processed, result.succeeded, result.failed), (1, 1, 0))
        jobs_cls.return_value.try_start_anki_note_job.assert_called_once_with(
            21, "WordModel", "anki_notes"
        )
        connector.sync.assert_not_called()

    def test_failure_is_recorded_and_processing_continues(self):
        note = {
            "noteId": 12,
            "modelName": "WordModel",
            "fields": {
                "Word": {"value": "あて"},
                "Reading": {"value": ""},
                "MeaningDestination": {"value": ""},
                "ImagePrompt": {"value": ""},
                "Image": {"value": ""},
                "Reading_Audio": {"value": ""},
            },
        }
        second_note = {**note, "noteId": 13, "fields": {**note["fields"], "Word": {"value": "成功"}}}
        events = []
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.anki_notes.AnkiConnector") as connector_cls,
            patch("batch.anki_notes.JobManager") as jobs_cls,
            patch("batch.anki_notes.WordGenerator") as word_cls,
            patch("batch.anki_notes.ImageGenerator") as image_cls,
            patch("batch.anki_notes.TTSGenerator") as tts_cls,
            patch("batch.anki_notes.log") as log_mock,
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = [note, second_note]
            jobs = jobs_cls.return_value
            jobs.try_start_anki_note_job.side_effect = [101, 102]

            def generate(row, fields, model_fields):
                events.append("word")
                if row["Word"] == "あて":
                    raise RuntimeError("chat failure")
                fields["Reading"] = "あて"

            word_cls.return_value.generate_missing_fields.side_effect = generate
            image_cls.return_value.generate_for_note.side_effect = (
                lambda *args: events.append("image") or "<img>"
            )
            tts_cls.return_value.generate_audio_fields.side_effect = (
                lambda fields, *_args, **_kwargs: events.append("tts") or fields
            )
            batch = AnkiNotesBatch(
                {"anki_import": {"note_types": {"word": "WordModel"}}},
                Path(tmp) / "config.json",
            )
            result = batch.process()

        self.assertEqual((result.processed, result.succeeded, result.failed), (2, 1, 1))
        self.assertEqual(events, ["word", "word", "tts"])
        self.assertEqual(
            [call.args[:2] for call in jobs.try_start_anki_note_job.call_args_list],
            [(12, "WordModel"), (13, "WordModel")],
        )
        jobs.finish_anki_note_job.assert_any_call(101, "error", response="chat failure")
        jobs.finish_anki_note_job.assert_any_call(
            102,
            "completed",
            result={
                "anki_note_id": 13,
                "word": "成功",
                "image": "",
                "audio": {},
            },
        )
        self.assertTrue(
            any(
                "status=updated filled_fields=Reading" in call.args[2]
                for call in log_mock.call_args_list
            )
        )

    def test_skipped_note_does_not_create_batch_job(self):
        note = {
            "noteId": 14,
            "modelName": "WordModel",
            "fields": {"Word": {"value": "あて"}, "Reading": {"value": "あて"}},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.anki_notes.AnkiConnector") as connector_cls,
            patch("batch.anki_notes.JobManager") as jobs_cls,
            patch("batch.anki_notes.WordGenerator"),
            patch("batch.anki_notes.ImageGenerator"),
            patch("batch.anki_notes.TTSGenerator"),
            patch("batch.anki_notes.log"),
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = [note]
            batch = AnkiNotesBatch(
                {"anki_import": {"note_types": {"word": "WordModel"}}},
                Path(tmp) / "config.json",
            )
            batch.word_creator.generate_missing_fields = lambda *_args: None

            result = batch.process()

        self.assertEqual((result.processed, result.succeeded, result.failed), (0, 0, 0))
        jobs_cls.return_value.try_start_anki_note_job.assert_not_called()

    def test_no_change_log_reports_planned_fields_not_missing_fields(self):
        note = {
            "noteId": 16,
            "modelName": "WordModel",
            "fields": {"Word": {"value": "あて"}, "Reading": {"value": ""}},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.anki_notes.AnkiConnector") as connector_cls,
            patch("batch.anki_notes.JobManager") as jobs_cls,
            patch("batch.anki_notes.WordGenerator"),
            patch("batch.anki_notes.ImageGenerator"),
            patch("batch.anki_notes.TTSGenerator"),
            patch("batch.anki_notes.log") as log_mock,
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = [note]
            jobs_cls.return_value.try_start_anki_note_job.return_value = 101
            batch = AnkiNotesBatch(
                {"anki_import": {"note_types": {"word": "WordModel"}}},
                Path(tmp) / "config.json",
            )

            result = batch.process()

        self.assertEqual((result.processed, result.succeeded, result.failed), (0, 0, 0))
        self.assertTrue(
            any(
                "reason=no_fields_changed planned_fields=Reading" in call.args[2]
                for call in log_mock.call_args_list
            )
        )
        self.assertTrue(
            any(
                "scan total_notes=1 candidates=1 selected=1 limit=unlimited"
                in call.args[2]
                for call in log_mock.call_args_list
            )
        )

    def test_locked_note_is_skipped_without_running_generators(self):
        note = {
            "noteId": 15,
            "modelName": "WordModel",
            "fields": {"Word": {"value": "あて"}, "Reading": {"value": ""}},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.anki_notes.AnkiConnector") as connector_cls,
            patch("batch.anki_notes.JobManager") as jobs_cls,
            patch("batch.anki_notes.WordGenerator") as word_cls,
            patch("batch.anki_notes.ImageGenerator"),
            patch("batch.anki_notes.TTSGenerator"),
            patch("batch.anki_notes.log") as log_mock,
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = [note]
            jobs_cls.return_value.try_start_anki_note_job.return_value = None
            batch = AnkiNotesBatch(
                {"anki_import": {"note_types": {"word": "WordModel"}}},
                Path(tmp) / "config.json",
            )

            result = batch.process()

        self.assertEqual((result.processed, result.succeeded, result.failed), (0, 0, 0))
        word_cls.return_value.generate_missing_fields.assert_not_called()
        jobs_cls.return_value.finish_anki_note_job.assert_not_called()
        self.assertTrue(
            any("reason=locked_by_task_id" in call.args[2] for call in log_mock.call_args_list)
        )

    def test_logger_creates_timestamped_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = log("anki_notes_test", tmp, "note=1 status=completed")
            self.assertRegex(path.name, r"^\d{14}_anki_notes_test\.log$")
            self.assertIn("INFO note=1 status=completed", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
