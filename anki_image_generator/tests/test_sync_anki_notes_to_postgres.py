from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from helpers.sync_anki_notes_to_postgres import (
    _flatten_fields,
    _note_target,
    sync_notes,
)


class TestSyncAnkiNotesToPostgres(unittest.TestCase):
    def setUp(self):
        self.config = {
            "pgsql_schema": "public",
            "pgsql_kanji_table": "anki_kanji",
            "pgsql_word_table": "anki_word",
            "anki_import": {
                "api_url": "http://anki",
                "note_types": {"word": "WordModel", "kanji": "KanjiModel"},
            },
        }

    def test_routes_by_model_and_flattens_fields(self):
        note = {
            "modelName": "KanjiModel",
            "fields": {"Kanji": {"value": "良"}, "MeaningVI": {"value": "tốt"}},
        }
        fields = _flatten_fields(note)
        self.assertEqual(fields, {"Kanji": "良", "MeaningVI": "tốt"})
        self.assertEqual(_note_target(note, fields, self.config), ("kanji", "anki_kanji"))

    def test_replaces_tables_with_matching_notes_and_skips_unknown_model(self):
        notes = [
            {
                "noteId": 11,
                "modelName": "WordModel",
                "fields": {"Word": {"value": "良い"}, "Reading": {"value": "よい"}},
            },
            {
                "noteId": 12,
                "modelName": "KanjiModel",
                "fields": {"Kanji": {"value": "良"}, "MeaningVI": {"value": "tốt"}},
            },
            {"noteId": 13, "modelName": "Other", "fields": {"Other": {"value": "x"}}},
        ]
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [
            ("anki_note_id",),
            ("Word",),
            ("Kanji",),
            ("Reading",),
            ("MeaningVI",),
        ]
        cursor.rowcount = 0

        with (
            patch("helpers.sync_anki_notes_to_postgres.AnkiConnector") as connector_type,
            patch("helpers.sync_anki_notes_to_postgres.connect_pgsql", return_value=connection),
        ):
            connector_type.return_value.all_notes.return_value = notes
            result = sync_notes(self.config)

        self.assertEqual(
            result, {"scanned": 3, "synced": 2, "inserted": 2, "updated": 0, "skipped": 1}
        )
        inserts = [
            call.args[0]
            for call in cursor.execute.call_args_list
            if call.args[0].startswith("INSERT INTO")
        ]
        deletes = [
            call.args[0]
            for call in cursor.execute.call_args_list
            if call.args[0].startswith("DELETE FROM")
        ]
        self.assertEqual(
            set(deletes),
            {'DELETE FROM "public"."anki_word"', 'DELETE FROM "public"."anki_kanji"'},
        )
        self.assertEqual(len(inserts), 2)
        first_insert_index = next(
            index for index, call in enumerate(cursor.execute.call_args_list)
            if call.args[0].startswith("INSERT INTO")
        )
        self.assertTrue(
            all(
                index < first_insert_index
                for index, call in enumerate(cursor.execute.call_args_list)
                if call.args[0].startswith("DELETE FROM")
            )
        )
        connection.commit.assert_called_once()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
