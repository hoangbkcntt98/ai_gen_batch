from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from batch.images import ImagesBatch


class TestImagesBatch(unittest.TestCase):
    def test_selects_blank_images_limits_candidates_and_runs_in_steps(self):
        notes = [
            {
                "noteId": 1,
                "fields": {
                    "Word": {"value": "語彙"},
                    "ImagePrompt": {"value": "prompt one"},
                    "Image": {"value": ""},
                },
            },
            {
                "noteId": 2,
                "fields": {
                    "Word": {"value": "既存"},
                    "ImagePrompt": {"value": "prompt two"},
                    "Image": {"value": "<img src='existing.png'>"},
                },
            },
            {
                "noteId": 3,
                "fields": {
                    "Word": {"value": "promptなし"},
                    "ImagePrompt": {"value": ""},
                    "Image": {"value": ""},
                },
            },
            {
                "noteId": 4,
                "fields": {
                    "Kanji": {"value": "字"},
                    "ImagePrompt": {"value": "prompt four"},
                    "Image": {"value": ""},
                },
            },
            {
                "noteId": 5,
                "fields": {
                    "ImagePrompt": {"value": "prompt five"},
                    "Image": {"value": ""},
                },
            },
        ]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("batch.images.AnkiConnector") as connector_cls,
            patch("batch.images.ImageGenerator") as image_cls,
            patch("batch.images.log"),
        ):
            connector = connector_cls.return_value
            connector.all_notes.return_value = notes
            image_cls.return_value.generate_for_note.return_value = '<img src="generated.png">'
            batch = ImagesBatch(
                {"services": {"image_generator": {"step": 1}}},
                Path(tmp) / "config.json",
            )

            result = batch.process(limit=2)

        self.assertEqual(
            result,
            {
                "scanned": 5,
                "candidates": 2,
                "selected": 2,
                "processed": 2,
                "succeeded": 2,
                "failed": 0,
                "skipped_no_prompt": 1,
                "skipped_no_identity": 1,
            },
        )
        self.assertEqual(connector.sync.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in image_cls.return_value.generate_for_note.call_args_list],
            ["prompt one", "prompt four"],
        )
        self.assertEqual(
            [call.args[0] for call in connector.update_note_fields.call_args_list],
            [1, 4],
        )


if __name__ == "__main__":
    unittest.main()
