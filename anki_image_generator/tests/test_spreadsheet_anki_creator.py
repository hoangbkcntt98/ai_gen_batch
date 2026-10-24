from pathlib import Path
import unittest
from unittest.mock import patch

from spreadsheet_anki_creator import create_notes


class TestSpreadsheetStatusFilter(unittest.TestCase):
    def setUp(self):
        self.config = {
            "anki_import": {
                "note_types": {"word": "WordModel"},
            },
        }
        self.rows = [
            ("Sheet1", 2, {"Word": "語", "Status": ""}),
            ("Sheet1", 3, {"Word": "語", "Status": "PENDING"}),
            ("Sheet1", 4, {"Word": "語", "Status": "SYNC_OK"}),
        ]

    def test_status_column_only_processes_sync_ok_rows(self):
        with (
            patch("spreadsheet_anki_creator.AnkiConnector") as connector_cls,
            patch("spreadsheet_anki_creator.WordGenerator"),
            patch("spreadsheet_anki_creator.TTSGenerator"),
            patch("spreadsheet_anki_creator.ImageGenerator"),
        ):
            connector = connector_cls.return_value
            connector.call.side_effect = [["Default"], ["Word"]]
            connector.note_type_for.return_value = "WordModel"
            connector.deck_for_tags.return_value = "Default"
            connector.has_note_field_value.return_value = False

            result = create_notes(
                self.config,
                self.rows,
                Path("config.json"),
                dry_run=True,
            )

        self.assertEqual(result.skipped, 2)
        connector.has_note_field_value.assert_called_once_with(
            "WordModel", "Word", "語"
        )

    def test_missing_status_column_keeps_existing_behavior(self):
        with (
            patch("spreadsheet_anki_creator.AnkiConnector") as connector_cls,
            patch("spreadsheet_anki_creator.WordGenerator"),
            patch("spreadsheet_anki_creator.TTSGenerator"),
            patch("spreadsheet_anki_creator.ImageGenerator"),
        ):
            connector = connector_cls.return_value
            connector.call.side_effect = [["Default"], ["Word"]]
            connector.note_type_for.return_value = "WordModel"
            connector.deck_for_tags.return_value = "Default"
            connector.has_note_field_value.return_value = False

            result = create_notes(
                self.config,
                [("Sheet1", 2, {"Word": "語"})],
                Path("config.json"),
                dry_run=True,
            )

        self.assertEqual(result.skipped, 0)
        connector.has_note_field_value.assert_called_once_with(
            "WordModel", "Word", "語"
        )


class TestDuplicateDeckLabel(unittest.TestCase):
    def test_duplicate_gets_deck_suffix_after_word_generation(self):
        captured_generator_fields = []
        with (
            patch("spreadsheet_anki_creator.AnkiConnector") as connector_cls,
            patch("spreadsheet_anki_creator.WordGenerator") as word_cls,
            patch("spreadsheet_anki_creator.TTSGenerator"),
            patch("spreadsheet_anki_creator.ImageGenerator"),
        ):
            connector = connector_cls.return_value
            connector.call.side_effect = [["DeckA"], ["Word", "Reading"]]
            connector.note_type_for.return_value = "WordModel"
            connector.deck_for_tags.return_value = "DeckA"
            connector.has_note_field_value.side_effect = [True, False]
            connector.add_note.return_value = 123
            word_cls.return_value.generate_missing_fields.side_effect = (
                lambda _row, fields, *_args, **_kwargs: captured_generator_fields.append(
                    dict(fields)
                )
            )

            result = create_notes(
                {"anki_import": {"note_types": {"word": "WordModel"}}},
                [("Sheet1", 2, {"Word": "語", "Tags": "DeckA"})],
                Path("config.json"),
                skip_images=True,
                skip_tts=True,
            )

        self.assertEqual(result.created, 1)
        self.assertEqual(captured_generator_fields[0]["Word"], "語")
        added_fields = connector.add_note.call_args.args[2]
        self.assertEqual(added_fields["Word"], "語 (DeckA)")
        self.assertEqual(
            connector.has_note_field_value.call_args_list[1].args,
            ("WordModel", "Word", "語 (DeckA)"),
        )


if __name__ == "__main__":
    unittest.main()
