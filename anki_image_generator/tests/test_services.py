from __future__ import annotations

import base64
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.anki_connector import AnkiConnector
from services.google_services import GoogleSheetsService
from services.image_generator import ImageGenerator, _filename, request_image
from services.job_manager import JobManager, _identifier, table_for_object_type
from services.tts_generator import TTSGenerator
from services.word_generator import (
    WordGenerator,
    build_image_prompt,
    generate_missing_fields,
    generation_plan,
)
from spreadsheet_anki_creator import (
    _dict_rows,
    _note_fields,
    _read_spreadsheet,
    create_notes,
)


class TestAnkiConnector(unittest.TestCase):
    def test_add_note_sends_expected_payload(self):
        session = MagicMock()
        session.post.return_value.raise_for_status.return_value = None
        session.post.return_value.json.return_value = {"error": None, "result": 42}
        connector = AnkiConnector(session=session)

        result = connector.add_note("Deck", "WordModel", {"Word": "あて"}, ["anki"])

        self.assertEqual(result, 42)
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["action"], "addNote")
        self.assertEqual(payload["params"]["note"]["modelName"], "WordModel")
        self.assertEqual(payload["params"]["note"]["tags"], ["anki"])

    def test_deck_mapping_requires_exactly_one_match(self):
        connector = AnkiConnector()
        self.assertEqual(connector.deck_for_tags(["tag", "Deck"], ["Deck"]), "Deck")
        with self.assertRaises(ValueError):
            connector.deck_for_tags(["tag"], ["Deck"])
        with self.assertRaises(ValueError):
            connector.deck_for_tags(["Deck", "Other"], ["Deck", "Other"])


class TestGoogleSheetsService(unittest.TestCase):
    def test_reads_spreadsheet_tabs(self):
        credentials_module = types.ModuleType("google.oauth2.service_account")
        credentials_module.Credentials = MagicMock()
        discovery_module = types.ModuleType("googleapiclient.discovery")
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "properties": {"title": "Japanese"},
            "sheets": [{"properties": {"title": "Sheet1"}}],
        }
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["Word"], ["あて"]]
        }
        discovery_module.build = MagicMock(return_value=service)
        modules = {
            "google": types.ModuleType("google"),
            "google.oauth2": types.ModuleType("google.oauth2"),
            "google.oauth2.service_account": credentials_module,
            "googleapiclient": types.ModuleType("googleapiclient"),
            "googleapiclient.discovery": discovery_module,
        }
        with patch.dict(sys.modules, modules):
            title, tabs = GoogleSheetsService("account_service.json").read_spreadsheet("sheet-id")
        self.assertEqual(title, "Japanese")
        self.assertEqual(tabs, [("Sheet1", [["Word"], ["あて"]])])


class TestImageGenerator(unittest.TestCase):
    def test_request_image_decodes_and_validates_png(self):
        png = b"\x89PNG\r\n\x1a\n" + b"mock-data" + b"\x00\x00\x00\x00IEND\xaeB`\x82"
        encoded = base64.b64encode(png).decode("ascii")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"data": [{"b64_json": encoded}]}
        ).encode()
        with patch("services.image_generator.urllib.request.urlopen", return_value=response):
            data, extension = request_image(
                "prompt", {"services": {"image_generator": {
                    "image_model": "model", "image_api_base": "http://test"
                }}}
            )
        self.assertEqual(data, png)
        self.assertEqual(extension, ".png")

    def test_filename_and_generator_construction(self):
        self.assertEqual(_filename("あて word", 12, ".png"), "あて_word_12.png")
        generator = ImageGenerator({})
        self.assertTrue(generator.config_path.name == "config.json")


class TestJobManager(unittest.TestCase):
    def test_validates_table_and_object_type(self):
        manager = JobManager({"pgsql_kanji_table": "kanji", "pgsql_word_table": "words"})
        self.assertEqual(manager.job_table, '"public"."batch_job"')
        self.assertEqual(table_for_object_type("WORD", manager.tables), "words")
        with self.assertRaises(ValueError):
            _identifier("bad-name", "table")

    def test_note_job_claim_skips_existing_locked_processing_job(self):
        manager = JobManager({})
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        manager.connect = MagicMock(return_value=connection)

        job_id = manager.try_start_anki_note_job(42, "WordModel")

        self.assertIsNone(job_id)
        self.assertEqual(cursor.execute.call_count, 2)
        self.assertNotIn(
            "INSERT INTO",
            " ".join(str(call.args[0]) for call in cursor.execute.call_args_list),
        )
        connection.commit.assert_called_once()
        connection.close.assert_called_once()

    def test_note_job_claim_inserts_processing_job_when_unlocked(self):
        manager = JobManager({})
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [None, (99,)]
        manager.connect = MagicMock(return_value=connection)

        job_id = manager.try_start_anki_note_job(42, "WordModel")

        self.assertEqual(job_id, 99)
        self.assertIn("locked_by_task_id", cursor.execute.call_args.args[0])
        connection.commit.assert_called_once()
        connection.close.assert_called_once()


class TestTTSGenerator(unittest.TestCase):
    def test_generates_missing_audio_field(self):
        response = MagicMock(content=b"audio-bytes")
        response.raise_for_status.return_value = None
        post = MagicMock(return_value=response)
        stored = {}
        generator = TTSGenerator(
            {"services": {"tts_generator": {"api_base": "http://tts/v1", "model": "voice"}}},
            request_post=post,
        )

        fields = generator.generate_audio_fields(
            {"Reading": "あて", "Reading_Audio": ""},
            ["Reading", "Reading_Audio"],
            lambda name, data: stored.update({name: data}),
            note_id=7,
            identity="あて",
        )

        self.assertEqual(fields["Reading_Audio"], "[sound:あて_7.mp3]")
        self.assertEqual(len(stored), 1)
        self.assertEqual(base64.b64decode(next(iter(stored.values()))), b"audio-bytes")


    def test_does_not_generate_required_vocabulary_audio(self):
        post = MagicMock()
        stored = {}
        generator = TTSGenerator(
            {"services": {"tts_generator": {"api_base": "http://tts/v1", "model": "voice"}}},
            request_post=post,
        )

        fields = generator.generate_audio_fields(
            {"Kanji": "生", "RequiredVocabulary": "生きる", "RequiredVocabulary_Audio": ""},
            ["RequiredVocabulary_Audio"],
            lambda name, data: stored.update({name: data}),
            note_id=7,
            identity="生",
        )

        self.assertEqual(fields["RequiredVocabulary_Audio"], "")
        self.assertEqual(stored, {})
        post.assert_not_called()


class TestWordGenerator(unittest.TestCase):
    def test_generation_plan_and_image_prompt(self):
        plan = generation_plan(
            {"Word": "渋々"},
            {"Word": "渋々", "Reading": "", "ImagePrompt": "", "Reading_Audio": ""},
            ["Reading", "ImagePrompt", "Reading_Audio"],
        )
        self.assertEqual(plan, (["Reading"], ["ImagePrompt"], ["Reading_Audio"]))
        prompt = build_image_prompt("渋々", "reluctantly")
        self.assertIn('"渋々" ("reluctantly")', prompt)
        self.assertIn("no text, no watermark", prompt)

    def test_generation_plan_excludes_required_vocabulary_audio_for_kanji(self):
        plan = generation_plan(
            {"Kanji": "生"},
            {
                "MeaningJP": "生きる",
                "RequiredVocabulary": "生きる",
                "RequiredVocabulary_Audio": "",
            },
            ["MeaningJP", "RequiredVocabulary", "RequiredVocabulary_Audio"],
        )

        self.assertNotIn("RequiredVocabulary_Audio", plan[2])

    def test_generation_plan_ignores_audio_without_source_text(self):
        fields = {
            "Kanji": "沿",
            "MeaningJP": "沿う",
            "Example1_JP": "川に沿って歩く。",
            "Example3_JP": "",
            "Example3_Audio": "",
        }
        plan = generation_plan(
            {"Kanji": "沿"},
            fields,
            ["Kanji", "MeaningJP", "Example1_JP", "Example3_JP", "Example3_Audio"],
        )

        self.assertEqual(plan, ([], [], []))

    def test_generates_missing_text_fields(self):
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [{"message": {"content": '{"Reading":"あて","MeaningDestination":"to guess"}'}}]
        }
        with patch("services.word_generator.requests.post", return_value=response):
            fields = WordGenerator(
                {"services": {"word_generator": {
                    "chat_api_base": "http://chat/v1", "chat_model": "test"
                }}}
            ).generate_missing_fields(
                {"Word": "あて"}, {"Reading": "", "MeaningDestination": ""},
                ["Reading", "MeaningDestination"],
            )
        self.assertEqual(fields, {"Reading": "あて", "MeaningDestination": "to guess"})

    def test_empty_phonetic_part_is_filled_with_none_marker(self):
        fields = {
            "Kanji": "至",
            "MeaningJP": "到達する、極める",
            "PhoneticPart": "",
        }
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [{"message": {"content": '{"PhoneticPart":""}'}}]
        }

        with patch("services.word_generator.requests.post", return_value=response):
            generate_missing_fields(
                {"Kanji": "至"},
                fields,
                list(fields),
                {"services": {"word_generator": {
                    "chat_api_base": "http://chat/v1", "chat_model": "test"
                }}},
            )

        self.assertEqual(fields["PhoneticPart"], "無し")

    def test_missing_reading_triggers_full_word_schema_generation(self):
        fields = {
            "Word": "渋々",
            "Reading": "",
            "MeaningSource": "",
            "MeaningDestination": "reluctantly",
            "Example1_Source": "",
            "Example1_Destination": "",
            "ImagePrompt": "",
        }
        model_fields = list(fields)
        response = {
            "Word": "渋々",
            "Reading": "しぶしぶ",
            "MeaningSource": "unwillingly",
            "MeaningDestination": "reluctantly",
            "Usage": "",
            "Example1_Source": "彼は渋々同意した。",
            "Example1_Destination": "He reluctantly agreed.",
            "ImagePrompt": "",
            "Notes": "",
            "Chineses": "",
        }
        with patch("services.word_generator._call_json_api", return_value=response) as call:
            generate_missing_fields(
                {"Word": "渋々"},
                fields,
                model_fields,
                {"user_level": "invalid", "batch": {}},
            )

        prompt = call.call_args.args[0]
        self.assertIn('"user_level": "N3"', prompt)
        self.assertEqual(fields["MeaningSource"], "unwillingly")
        self.assertEqual(fields["MeaningDestination"], "reluctantly")
        self.assertEqual(fields["Example1_Source"], "彼は渋々同意した。")
        self.assertEqual(fields["Example1_Destination"], "He reluctantly agreed.")
        self.assertEqual(fields["ImagePrompt"], build_image_prompt("渋々", "reluctantly"))

    def test_translates_existing_word_example_without_creating_source(self):
        fields = {
            "Word": "渋々",
            "Reading": "しぶしぶ",
            "Example3_Source": "彼は渋々同意した。",
            "Example3_Destination": "",
        }
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [{
                "message": {"content": '{"Example3_Destination":"He reluctantly agreed."}'}
            }]
        }
        with patch("services.word_generator.requests.post", return_value=response):
            generate_missing_fields(
                {"Word": "渋々"}, fields, list(fields),
                {"services": {"word_generator": {
                    "chat_api_base": "http://chat/v1", "chat_model": "test"
                }}},
            )
        self.assertEqual(fields["Example3_Source"], "彼は渋々同意した。")
        self.assertEqual(fields["Example3_Destination"], "He reluctantly agreed.")

    def test_new_word_note_can_generate_complete_example_pair(self):
        fields = {
            "Word": "渋々",
            "Reading": "しぶしぶ",
            "Example1_Source": "",
            "Example1_Destination": "",
        }
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": (
                        '{"Example1_Source":"彼は渋々同意した。",'
                        '"Example1_Destination":"He reluctantly agreed."}'
                    )
                }
            }]
        }
        with patch("services.word_generator.requests.post", return_value=response):
            generate_missing_fields(
                {"Word": "渋々"}, fields, list(fields),
                {"services": {"word_generator": {
                    "chat_api_base": "http://chat/v1", "chat_model": "test"
                }}},
                allow_new_examples=True,
            )
        self.assertEqual(fields["Example1_Source"], "彼は渋々同意した。")
        self.assertEqual(fields["Example1_Destination"], "He reluctantly agreed.")

    def test_missing_fields_bootstraps_at_most_three_example_pairs(self):
        fields = {"Word": "渋々", "Reading": ""}
        for index in range(1, 7):
            fields[f"Example{index}_Source"] = ""
            fields[f"Example{index}_Destination"] = ""
        response = {
            "Word": "渋々",
            "Reading": "しぶしぶ",
            "Example1_Source": "source 1",
            "Example1_Destination": "destination 1",
            "Example2_Source": "source 2",
            "Example2_Destination": "destination 2",
            "Example3_Source": "source 3",
            "Example3_Destination": "destination 3",
            "Example4_Source": "must not be saved",
            "Example4_Destination": "must not be saved",
        }
        with patch("services.word_generator._call_json_api", return_value=response) as call:
            generate_missing_fields(
                {"Word": "渋々"}, fields, list(fields), {"services": {}}
            )

        self.assertEqual(fields["Example3_Source"], "source 3")
        self.assertEqual(fields["Example3_Destination"], "destination 3")
        self.assertEqual(fields["Example4_Source"], "")
        self.assertEqual(fields["Example4_Destination"], "")
        prompt = call.call_args.args[0]
        self.assertIn('"Example3_Source"', prompt)
        self.assertNotIn('"Example4_Source"', prompt)

    def test_existing_kanji_pair_disables_new_pairs_but_allows_translation(self):
        fields = {
            "Kanji": "生",
            "MeaningJP": "いきる",
            "Vocabulary1_JP": "生きる（いきる）",
            "Vocabulary1_VN": "sống",
            "Example2_JP": "彼は毎日生きている。",
            "Example2_VN": "",
            "Example3_JP": "",
            "Example3_VN": "",
        }
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": (
                        '{"Example2_VN":"Anh ấy sống mỗi ngày.",'
                        '"Example3_JP":"new source","Example3_VN":"new translation"}'
                    )
                }
            }]
        }
        config = {"services": {"word_generator": {
            "chat_api_base": "http://chat/v1", "chat_model": "test"
        }}}
        with patch("services.word_generator.requests.post", return_value=response) as post:
            generate_missing_fields({"Kanji": "生"}, fields, list(fields), config)

        self.assertEqual(fields["Example2_VN"], "Anh ấy sống mỗi ngày.")
        self.assertEqual(fields["Example3_JP"], "")
        self.assertEqual(fields["Example3_VN"], "")
        requested = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("Example2_VN", requested)
        self.assertNotIn("Example3_JP", requested)

    def test_new_kanji_note_can_generate_complete_example_pair(self):
        fields = {
            "Kanji": "生",
            "MeaningJP": "いきる",
            "Example1_JP": "",
            "Example1_VN": "",
        }
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": (
                        '{"Example1_JP":"彼は毎日生きている。",'
                        '"Example1_VN":"Anh ấy sống mỗi ngày."}'
                    )
                }
            }]
        }
        with patch("services.word_generator.requests.post", return_value=response):
            generate_missing_fields(
                {"Kanji": "生"}, fields, list(fields),
                {"services": {"word_generator": {
                    "chat_api_base": "http://chat/v1", "chat_model": "test"
                }}},
                allow_new_examples=True,
            )
        self.assertEqual(fields["Example1_JP"], "彼は毎日生きている。")
        self.assertEqual(fields["Example1_VN"], "Anh ấy sống mỗi ngày.")

    def test_kanji_vocabulary_destination_requires_source(self):
        fields = {
            "Kanji": "生",
            "MeaningJP": "いきる",
            "Vocabulary1_JP": "生きる（いきる）",
            "Vocabulary1_VN": "sống",
            "Vocabulary3_JP": "",
            "Vocabulary3_VN": "",
        }
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [{"message": {"content": '{"Vocabulary3_VN":"sống"}'}}]
        }
        config = {"services": {"word_generator": {
            "chat_api_base": "http://chat/v1", "chat_model": "test"
        }}}
        with patch(
            "services.word_generator.requests.post",
            return_value=response,
        ) as post:
            generate_missing_fields({"Kanji": "生"}, fields, list(fields), config)

        self.assertEqual(fields["Vocabulary3_JP"], "")
        self.assertEqual(fields["Vocabulary3_VN"], "")
        post.assert_not_called()

        fields["Vocabulary3_JP"] = "生きる（いきる）"
        with patch("services.word_generator.requests.post", return_value=response) as post:
            generate_missing_fields({"Kanji": "生"}, fields, list(fields), config)

        self.assertEqual(fields["Vocabulary3_JP"], "生きる（いきる）")
        self.assertEqual(fields["Vocabulary3_VN"], "sống")
        requested_fields = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("Return exactly these keys: Vocabulary3_VN.", requested_fields)

    def test_existing_first_vocabulary_prevents_adding_later_slot(self):
        fields = {
            "Kanji": "生",
            "MeaningJP": "いきる",
            "Vocabulary1_JP": "",
            "Vocabulary1_VN": "",
            "Vocabulary3_JP": "",
            "Vocabulary3_VN": "",
        }
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": (
                        '{"Vocabulary1_JP":"生きる（いきる）",'
                        '"Vocabulary3_JP":"人生（じんせい）",'
                        '"Vocabulary3_VN":"cuộc đời"}'
                    )
                }
            }]
        }
        config = {"services": {"word_generator": {
            "chat_api_base": "http://chat/v1", "chat_model": "test"
        }}}
        with patch("services.word_generator.requests.post", return_value=response):
            generate_missing_fields({"Kanji": "生"}, fields, list(fields), config)

        self.assertEqual(fields["Vocabulary1_JP"], "")
        self.assertEqual(fields["Vocabulary3_JP"], "")
        self.assertEqual(fields["Vocabulary3_VN"], "")

    def test_missing_meaning_jp_triggers_full_kanji_schema(self):
        fields = {
            "Kanji": "生",
            "MeaningJP": "",
            "RequiredVocabulary": "",
            "Vocabulary1_JP": "",
            "Vocabulary1_VN": "",
            "Example3_JP": "",
            "Example3_VN": "",
        }
        response = {
            "Kanji": "生",
            "MeaningJP": "いきる",
            "MeaningVI": "sống",
            "RequiredVocabulary": "生きる",
            "Vocabulary1_JP": "生きる（いきる）",
            "Vocabulary1_VN": "sống",
            "Example3_JP": "彼は毎日生きている。",
            "Example3_VN": "Anh ấy sống mỗi ngày.",
        }
        with patch("services.word_generator._call_json_api", return_value=response) as call:
            generate_missing_fields(
                {"Kanji": "生", "RequiredVocabulary": "生きる"},
                fields,
                list(fields),
                {"services": {}},
            )

        prompt = call.call_args.args[0]
        self.assertIn('"required_vocabulary": "生きる"', prompt)
        self.assertEqual(fields["RequiredVocabulary"], "生きる")
        self.assertEqual(fields["Vocabulary1_JP"], "")
        self.assertEqual(fields["Example3_JP"], "彼は毎日生きている。")
        self.assertEqual(fields["Example3_VN"], "Anh ấy sống mỗi ngày.")
        self.assertNotIn("ImagePrompt", fields)


class TestSpreadsheetCreator(unittest.TestCase):
    def test_input_accepts_google_sheets_url(self):
        with patch.object(GoogleSheetsService, "read_spreadsheet", return_value=("Sheet", [])) as read:
            title, tabs = _read_spreadsheet(
                "https://docs.google.com/spreadsheets/d/sheet-id/edit?usp=sharing",
                None,
                Path("account_service.json"),
            )
        self.assertEqual((title, tabs), ("Sheet", []))
        read.assert_called_once_with("sheet-id")

    def test_maps_sheet_rows_and_field_aliases(self):
        rows = _dict_rows([("Sheet1", [
            ["Word", "Meaning_Source", "Tags"],
            ["あて", "guess", "anki;Deck"],
        ])])
        self.assertEqual(rows[0][2]["Word"], "あて")
        fields = _note_fields(rows[0][2], ["Word", "MeaningSource"])
        self.assertEqual(fields, {"Word": "あて", "MeaningSource": "guess"})

    def test_runs_generators_and_updates_new_note(self):
        connector = MagicMock()
        connector.call.side_effect = [["Deck"], ["Word", "Reading", "ImagePrompt", "Reading_Audio", "Image"]]
        connector.note_type_for.return_value = "WordModel"
        connector.deck_for_tags.return_value = "Deck"
        connector.add_note.return_value = 123
        config = {
            "anki_import": {"note_types": {"word": "WordModel"}},
            "services": {"image_generator": {"media_dir": ""}},
        }
        rows = [("Sheet1", 2, {"Word": "あて", "Tags": "anki;Deck"})]

        with (
            patch("spreadsheet_anki_creator.AnkiConnector", return_value=connector),
            patch("spreadsheet_anki_creator.WordGenerator") as word_cls,
            patch("spreadsheet_anki_creator.TTSGenerator") as tts_cls,
            patch("spreadsheet_anki_creator.ImageGenerator") as image_cls,
        ):
            word_cls.return_value.generate_missing_fields.side_effect = (
                lambda row, fields, names, **kwargs: fields.update(
                    {"ImagePrompt": "prompt"}
                )
            )
            tts_cls.return_value.generate_audio_fields.side_effect = (
                lambda fields, names, store, **kwargs: fields.update(
                    {"Reading_Audio": "[sound:word_123_reading_audio.mp3]"}
                )
            )
            image_cls.return_value.generate_for_note.return_value = '<img src="word_123.png">'

            result = create_notes(config, rows, Path("config.json"))

        self.assertEqual(result.created, 1)
        self.assertEqual(result.failed, 0)
        word_cls.return_value.generate_missing_fields.assert_called_once()
        self.assertTrue(
            word_cls.return_value.generate_missing_fields.call_args.kwargs[
                "allow_new_examples"
            ]
        )
        connector.update_note_fields.assert_called_once_with(
            123,
            {
                "Reading_Audio": "[sound:word_123_reading_audio.mp3]",
                "Image": '<img src="word_123.png">',
            },
        )


    def test_config_can_skip_image_and_tts_generation(self):
        connector = MagicMock()
        connector.call.side_effect = [["Deck"], ["Word", "Reading", "ImagePrompt", "Reading_Audio", "Image"]]
        connector.note_type_for.return_value = "WordModel"
        connector.deck_for_tags.return_value = "Deck"
        connector.add_note.return_value = 123
        config = {
            "anki_import": {"note_types": {"word": "WordModel"}},
            "services": {
                "spreadsheet_anki_creator": {"skip_images": True, "skip_tts": True}
            },
        }
        rows = [("Sheet1", 2, {"Word": "あて", "Tags": "anki;Deck"})]

        with (
            patch("spreadsheet_anki_creator.AnkiConnector", return_value=connector),
            patch("spreadsheet_anki_creator.WordGenerator") as word_cls,
            patch("spreadsheet_anki_creator.TTSGenerator") as tts_cls,
            patch("spreadsheet_anki_creator.ImageGenerator") as image_cls,
        ):
            word_cls.return_value.generate_missing_fields.side_effect = (
                lambda _row, fields, _names, **kwargs: fields.update(
                    {"ImagePrompt": "prompt"}
                )
            )
            result = create_notes(config, rows, Path("config.json"))

        self.assertEqual((result.created, result.failed), (1, 0))
        tts_cls.return_value.generate_audio_fields.assert_not_called()
        image_cls.return_value.generate_for_note.assert_not_called()
        connector.update_note_fields.assert_not_called()

SERVICE_TESTS = {
    "anki": TestAnkiConnector,
    "google": TestGoogleSheetsService,
    "image": TestImageGenerator,
    "jobs": TestJobManager,
    "tts": TestTTSGenerator,
    "word": TestWordGenerator,
    "creator": TestSpreadsheetCreator,
}


def run_tests(service: str = "all") -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    selected = SERVICE_TESTS if service == "all" else {service: SERVICE_TESTS[service]}
    for test_case in selected.values():
        suite.addTests(loader.loadTestsFromTestCase(test_case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run offline service tests.")
    parser.add_argument("service", nargs="?", choices=["all", *SERVICE_TESTS], default="all")
    raise SystemExit(run_tests(parser.parse_args().service))
