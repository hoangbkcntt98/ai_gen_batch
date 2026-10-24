import unittest
from unittest.mock import patch

from helpers.sync_ankiweb_to_local import sync_from_ankiweb


class TestSyncAnkiwebToLocal(unittest.TestCase):
    def test_uses_configured_ankiconnect_url_and_triggers_sync(self):
        with patch("helpers.sync_ankiweb_to_local.AnkiConnector") as connector_cls:
            connector_cls.return_value.sync.return_value = "ok"

            result = sync_from_ankiweb(
                {"anki_import": {"api_url": "http://127.0.0.1:8765"}},
                timeout=900,
            )

        self.assertEqual(result, "ok")
        connector_cls.assert_called_once_with("http://127.0.0.1:8765")
        connector_cls.return_value.sync.assert_called_once_with(timeout=900)


if __name__ == "__main__":
    unittest.main()
