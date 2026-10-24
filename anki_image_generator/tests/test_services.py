from __future__ import annotations

import base64
import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from services.anki_connector import AnkiConnector
from services.google_services import GoogleSheetsService
from services.image_generator import ImageGenerator, _filename, request_image
from services.job_manager import JobManager, _identifier, table_for_object_type
from services.tts_generator import TTSGenerator
from services.word_generator import WordGenerator, build_image_prompt, generation_plan


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
                "prompt", {"image_model": "model", "image_api_base": "http://test"}
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


class TestTTSGenerator(unittest.TestCase):
    def test_generates_missing_audio_field(self):
        response = MagicMock(content=b"audio-bytes")
        response.raise_for_status.return_value = None
        post = MagicMock(return_value=response)
        stored = {}
        generator = TTSGenerator(
            {"batch": {"tts": {"api_base": "http://tts/v1", "model": "voice"}}},
            request_post=post,
        )

        fields = generator.generate_audio_fields(
            {"Reading": "あて", "Reading_Audio": ""},
            ["Reading", "Reading_Audio"],
            lambda name, data: stored.update({name: data}),
            note_id=7,
            identity="word",
        )

        self.assertEqual(fields["Reading_Audio"], "[sound:word_7_reading_audio.mp3]")
        self.assertEqual(len(stored), 1)
        self.assertEqual(base64.b64decode(next(iter(stored.values()))), b"audio-bytes")


class TestWordGenerator(unittest.TestCase):
    def test_generation_plan_and_image_prompt(self):
        plan = generation_plan(
            {"Word": "渋々"},
            {"Reading": "", "ImagePrompt": "", "Reading_Audio": ""},
            ["Reading", "ImagePrompt", "Reading_Audio"],
        )
        self.assertEqual(plan, (["Reading"], ["ImagePrompt"], ["Reading_Audio"]))
        prompt = build_image_prompt("渋々", "reluctantly")
        self.assertIn('"渋々" ("reluctantly")', prompt)
        self.assertIn("no text, no watermark", prompt)

    def test_generates_missing_text_fields(self):
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [{"message": {"content": '{"Reading":"あて","MeaningDestination":"to guess"}'}}]
        }
        with patch("services.word_generator.requests.post", return_value=response):
            fields = WordGenerator(
                {"batch": {"word_kanji_generate": {
                    "chat_api_base": "http://chat/v1", "chat_model": "test"
                }}}
            ).generate_missing_fields(
                {"Word": "あて"}, {"Reading": "", "MeaningDestination": ""},
                ["Reading", "MeaningDestination"],
            )
        self.assertEqual(fields, {"Reading": "あて", "MeaningDestination": "to guess"})


SERVICE_TESTS = {
    "anki": TestAnkiConnector,
    "google": TestGoogleSheetsService,
    "image": TestImageGenerator,
    "jobs": TestJobManager,
    "tts": TestTTSGenerator,
    "word": TestWordGenerator,
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
