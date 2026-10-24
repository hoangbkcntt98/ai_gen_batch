from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from helpers.download_oci_to_media_dir import (
    _object_endpoint_and_prefix,
    download_objects,
)


class TestDownloadOciToMediaDir(unittest.TestCase):
    def test_splits_configured_object_prefix_from_par_endpoint(self):
        endpoint, prefix = _object_endpoint_and_prefix(
            "https://storage.example/p/secret/n/ns/b/bucket/o/images/"
        )
        self.assertEqual(
            endpoint, "https://storage.example/p/secret/n/ns/b/bucket/o/"
        )
        self.assertEqual(prefix, "images/")

    def test_normalizes_prefix_without_trailing_slash(self):
        _, prefix = _object_endpoint_and_prefix(
            "https://storage.example/p/secret/n/ns/b/bucket/o/images"
        )
        self.assertEqual(prefix, "images/")

    def test_downloads_images_and_skips_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            media_dir = Path(directory) / "media"
            media_dir.mkdir()
            (media_dir / "old.png").write_bytes(b"old")
            config = {
                "services": {
                    "image_generator": {
                        "upload_provider": "oci",
                        "image_base_path": (
                            "https://storage.example/p/secret/n/ns/b/bucket/o/images/"
                        ),
                        "media_dir": str(media_dir),
                    }
                }
            }
            listing = MagicMock(status_code=200)
            listing.json.return_value = {
                "objects": [
                    {"name": "images/new.png"},
                    {"name": "images/old.png"},
                    {"name": "images/readme.txt"},
                ]
            }
            image = MagicMock(status_code=200, content=b"png-bytes")
            with patch(
                "helpers.download_oci_to_media_dir.requests.get",
                side_effect=[listing, image],
            ) as get:
                result = download_objects(config, Path(directory) / "config.json")

            self.assertEqual(
                result, {"listed": 3, "downloaded": 1, "skipped": 2, "failed": 0}
            )
            self.assertEqual(
                (media_dir / "new.png").read_bytes(), b"png-bytes"
            )
            self.assertEqual((media_dir / "old.png").read_bytes(), b"old")
            self.assertEqual(get.call_args_list[0].kwargs["params"]["prefix"], "images/")
            self.assertIn(
                "/o/images/new.png", get.call_args_list[1].args[0]
            )

    def test_does_not_include_par_token_in_http_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "services": {
                    "image_generator": {
                        "upload_provider": "oci",
                        "image_base_path": (
                            "https://storage.example/p/secret-token/n/ns/b/bucket/o/images/"
                        ),
                        "media_dir": directory,
                    }
                }
            }
            response = MagicMock(status_code=403)
            response.text = "sensitive response"
            with patch(
                "helpers.download_oci_to_media_dir.requests.get",
                return_value=response,
            ), self.assertRaisesRegex(RuntimeError, "HTTP 403") as error:
                download_objects(config, Path(directory) / "config.json")
            self.assertNotIn("secret-token", str(error.exception))
            self.assertNotIn("sensitive response", str(error.exception))
    def test_reports_par_listing_requirements_for_404(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "services": {"image_generator": {
                    "upload_provider": "oci",
                    "image_base_path": (
                        "https://storage.example/p/secret-token/n/ns/b/bucket/o/images/"
                    ),
                    "media_dir": directory,
                }}
            }
            with patch(
                "helpers.download_oci_to_media_dir.requests.get",
                return_value=MagicMock(status_code=404),
            ), self.assertRaisesRegex(RuntimeError, "allows object listing"):
                download_objects(config, Path(directory) / "config.json")


if __name__ == "__main__":
    unittest.main()
