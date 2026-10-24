from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from helpers.convert_anki_image_par_links import convert_notes


class TestConvertAnkiImageParLinks(unittest.TestCase):
    def setUp(self):
        self.config = {
            "anki_import": {"api_url": "http://127.0.0.1:8765"},
            "services": {"image_generator": {
                "image_base_path": (
                    "https://storage.example/p/secret/n/ns/b/bucket/o/images/"
                ),
                "image_field": "Image",
            }},
        }
        self.note = {
            "noteId": 7,
            "fields": {
                "Image": {
                    "value": (
                        '<img src="https://storage.example/p/secret/n/ns/'
                        'b/bucket/o/images/kanji%20one.png">'
                    )
                }
            },
        }

    def test_replaces_par_url_with_local_filename(self):
        connector = MagicMock()
        connector.all_notes.return_value = [self.note]
        with patch(
            "helpers.convert_anki_image_par_links.AnkiConnector",
            return_value=connector,
        ):
            result = convert_notes(self.config)

        self.assertEqual(result, {"notes": 1, "updated": 1, "skipped": 0, "failed": 0})
        connector.update_note_fields.assert_called_once_with(
            7, {"Image": '<img src="kanji one.png">'}
        )

    def test_dry_run_does_not_download_or_update(self):
        connector = MagicMock()
        connector.all_notes.return_value = [self.note]
        with patch(
            "helpers.convert_anki_image_par_links.AnkiConnector",
            return_value=connector,
        ):
            result = convert_notes(self.config, dry_run=True)

        self.assertEqual(result, {"notes": 1, "updated": 1, "skipped": 0, "failed": 0})
        connector.store_media_file.assert_not_called()
        connector.update_note_fields.assert_not_called()


if __name__ == "__main__":
    unittest.main()
