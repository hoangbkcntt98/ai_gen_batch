from pathlib import Path
import unittest
from unittest.mock import patch

from spreadsheet_anki_creator import _read_inputs, create_notes


class TestMultipleInputs(unittest.TestCase):
    def test_reads_and_combines_files_and_spreadsheet_ids_in_order(self):
        first = ("words.csv", [("words", [["Word"], ["語"]])])
        second = ("sheet", [("Tab1", [["Kanji"], ["字"]])])
        with patch(
            "spreadsheet_anki_creator._read_spreadsheet",
            side_effect=[first, second],
        ) as read_source:
            title, tabs = _read_inputs(
                ["words.csv"],
                ["sheet-id"],
                Path("account_service.json"),
            )

        self.assertEqual(title, "words.csv, sheet")
        self.assertEqual(tabs, [*first[1], *second[1]])
        self.assertEqual(
            read_source.call_args_list[0].args,
            ("words.csv", None, Path("account_service.json")),
        )
        self.assertEqual(
            read_source.call_args_list[1].args,
            (None, "sheet-id", Path("account_service.json")),
        )

    def test_requires_at_least_one_input(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            _read_inputs([], [], Path("account_service.json"))


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
    def test_duplicate_word_is_skipped_without_deck_suffix_or_generation(self):
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
            connector.has_note_field_value.return_value = True

            result = create_notes(
                {"anki_import": {"note_types": {"word": "WordModel"}}},
                [("Sheet1", 2, {"Word": "語", "Tags": "DeckA"})],
                Path("config.json"),
                skip_images=True,
                skip_tts=True,
            )

        self.assertEqual((result.created, result.skipped), (0, 1))
        connector.has_note_field_value.assert_called_once_with(
            "WordModel", "Word", "語"
        )
        connector.add_note.assert_not_called()
        word_cls.return_value.generate_missing_fields.assert_not_called()


class TestDeferredGeneration(unittest.TestCase):
    def test_defer_generation_skips_ai_but_builds_kanji_image_prompt(self):
        with (
            patch("spreadsheet_anki_creator.AnkiConnector") as connector_cls,
            patch("spreadsheet_anki_creator.WordGenerator") as word_cls,
            patch("spreadsheet_anki_creator.TTSGenerator"),
            patch("spreadsheet_anki_creator.ImageGenerator"),
            patch(
                "spreadsheet_anki_creator.build_image_prompt",
                return_value="generated prompt",
            ) as build_prompt,
        ):
            connector = connector_cls.return_value
            connector.call.side_effect = [
                ["DeckA"],
                ["Kanji", "MeaningJP", "ImagePrompt"],
            ]
            connector.note_type_for.return_value = "KanjiModel"
            connector.deck_for_tags.return_value = "DeckA"
            connector.has_note_field_value.return_value = False
            connector.add_note.return_value = 123

            result = create_notes(
                {"anki_import": {"note_types": {"kanji": "KanjiModel"}}},
                [
                    (
                        "Sheet1",
                        2,
                        {"Kanji": "字", "MeaningJP": "文字", "Tags": "DeckA"},
                    )
                ],
                Path("config.json"),
                skip_images=True,
                skip_tts=True,
                defer_generation=True,
            )

        self.assertEqual(result.created, 1)
        word_cls.return_value.generate_missing_fields.assert_not_called()
        build_prompt.assert_called_once_with("字", "文字")
        connector.add_note.assert_called_once()
        self.assertEqual(
            connector.add_note.call_args.args[2]["ImagePrompt"],
            "generated prompt",
        )

if __name__ == "__main__":
    unittest.main()
