"""Exercise upload planning and failure boundaries without contacting AWS."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from experiments.agent_dataset_discovery.generators.config import Config
from experiments.agent_dataset_discovery.generators.corpus import create_corpus
from experiments.agent_dataset_discovery.setup_s3 import (
    destination,
    upload_corpus,
    upload_plan,
)


class S3SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def corpus(self, table_type="csv", with_index=True):
        folder = self.root / f"{table_type}-{with_index}"
        create_corpus(folder, Config(2, 4, with_index, 0, table_type))
        return folder

    def test_preserves_all_relative_paths_and_bytes_in_each_condition(self):
        for table_type in ("csv", "parquet"):
            for with_index in (True, False):
                with self.subTest(table_type=table_type, with_index=with_index):
                    folder = self.corpus(table_type, with_index)
                    files = upload_plan(folder, "corpus/")
                    actual = {}
                    client = Mock()
                    client.list_objects_v2.return_value = {}
                    client.upload_file.side_effect = (
                        lambda filename, bucket, key, actual=actual: actual.update(
                            {key: Path(filename).read_bytes()}
                        )
                    )
                    with contextlib.redirect_stderr(io.StringIO()):
                        upload_corpus(client, "bucket", "corpus/", files)
                    expected = {
                        "corpus/"
                        + path.relative_to(folder).as_posix(): path.read_bytes()
                        for path in folder.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(actual, expected)
                    self.assertEqual(
                        f"corpus/index/datasets.{table_type}" in actual, with_index
                    )

    def test_existing_prefix_is_rejected_before_any_upload(self):
        client = Mock()
        client.list_objects_v2.return_value = {
            "Contents": [{"Key": "corpus/index/datasets.csv"}]
        }
        with self.assertRaisesRegex(ValueError, "not empty"):
            upload_corpus(
                client, "bucket", "corpus/", upload_plan(self.corpus(), "corpus/")
            )
        client.upload_file.assert_not_called()

    def test_listing_failure_does_not_start_upload(self):
        client = Mock()
        client.list_objects_v2.side_effect = RuntimeError("Access denied")
        with self.assertRaisesRegex(RuntimeError, "Access denied"):
            upload_corpus(
                client, "bucket", "corpus/", upload_plan(self.corpus(), "corpus/")
            )
        client.upload_file.assert_not_called()

    def test_symlinks_and_missing_payloads_are_rejected(self):
        folder = self.corpus()
        link = folder / "linked-file"
        link.symlink_to(folder / "README.md")
        with self.assertRaisesRegex(ValueError, "regular files"):
            upload_plan(folder, "corpus/")
        link.unlink()
        next(folder.glob("studies/*/data/*.h5ad")).unlink()
        with self.assertRaises(FileNotFoundError):
            upload_plan(folder, "corpus/")

    def test_destination_requires_unambiguous_nonroot_prefix(self):
        self.assertEqual(
            destination("s3://bucket/corpus/nested/"), ("bucket", "corpus/nested/")
        )
        for uri in (
            "s3://bucket",
            "s3://bucket/",
            "https://bucket/corpus",
            "s3://bucket/../corpus",
            "s3://bucket//corpus",
            "s3://bucket/corpus?token=x",
            "s3://user@bucket/corpus",
        ):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                destination(uri)


if __name__ == "__main__":
    unittest.main()
