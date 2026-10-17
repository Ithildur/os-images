from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import images
import storage


class ImageStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.objects: dict = {}
        self.failure_key: str | None = None
        environment = patch.dict(os.environ, {
            "R2_BUCKET": "fixture", "GITHUB_REPOSITORY": "fixture/os-images", "GITHUB_SHA": "fixture",
            "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)
        transport = patch.object(storage, "aws", side_effect=self.aws)
        transport.start()
        self.addCleanup(transport.stop)

    def aws(self, *args: str) -> str:
        if args[:2] == ("s3api", "list-objects-v2"):
            prefix = args[args.index("--prefix") + 1]
            return json.dumps({"Contents": [
                {"Key": key, "Size": len(value["body"]), "LastModified": value["modified"]}
                for key, value in self.objects.items() if key.startswith(prefix)
            ]})
        if args[:2] == ("s3", "cp") and not args[2].startswith("s3://"):
            key = args[3].removeprefix("s3://fixture/")
            if key == self.failure_key:
                raise OSError("storage unavailable")
            self.objects[key] = {
                "body": Path(args[2]).read_bytes(), "modified": "2026-09-05T00:00:00Z",
                "content_type": args[args.index("--content-type") + 1],
                "cache_control": args[args.index("--cache-control") + 1],
            }
            return ""
        key = args[2].removeprefix("s3://fixture/")
        if args[:2] == ("s3", "cp"):
            Path(args[3]).write_bytes(self.objects[key]["body"])
        elif args[:2] == ("s3", "rm"):
            if key == self.failure_key:
                raise OSError("storage unavailable")
            del self.objects[key]
        else:
            raise AssertionError(args)
        return ""

    def build(self, image: str, digit: str, modified: str, *, attested: bool = True) -> dict:
        directory = self.root / image / digit
        directory.mkdir(parents=True)
        (directory / "packages.tsv").write_text("cloud-init\t1\n", encoding="utf-8")
        algorithm = images.recipe(image)["checksum_algorithm"]
        upstream = "ab" * (64 if algorithm == "sha512" else 32)
        images.record(argparse.Namespace(image=image, upstream_digest=f"{algorithm}:{upstream}",
                                         sha256=digit * 64, size_bytes=4, metadata=directory, output=directory))
        build = json.loads((directory / "build.json").read_text(encoding="utf-8"))
        key = build["object_key"]
        files = {
            key: b"disk", key.removesuffix(".qcow2") + ".sha256": (directory / "image.sha256").read_bytes(),
            build["provenance_key"]: (directory / "provenance.json").read_bytes(),
        }
        if attested:
            files[key.rsplit("/", 1)[0] + "/build.json"] = (directory / "build.json").read_bytes()
        self.objects.update({key: {"body": body, "modified": modified} for key, body in files.items()})
        return build

    def prune(self, selected: list[str], manifest: dict) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            storage.prune(selected, manifest)

    def test_cleanup_keeps_latest_and_published_for_each_system(self) -> None:
        old = self.build("debian12-latest", "1", "2026-09-01T00:00:00Z")
        published = self.build("debian12-latest", "2", "2026-09-02T00:00:00Z")
        latest = self.build("debian12-latest", "3", "2026-09-03T00:00:00Z")
        self.build("debian12-latest", "4", "2026-09-04T00:00:00Z", attested=False)
        self.build("ubuntu2404-latest", "2", "2026-09-01T00:00:00Z")
        ubuntu = self.build("ubuntu2404-latest", "5", "2026-09-02T00:00:00Z")
        unrelated = ["notes.txt", old["object_key"].rsplit("/", 1)[0] + "/operator-notes.txt"]
        for key in unrelated:
            self.objects[key] = {"body": b"keep", "modified": "2026-09-01T00:00:00Z"}
        expected = {key for key in self.objects if key in unrelated or any(
            key.startswith(build["object_key"].rsplit("/", 1)[0] + "/")
            for build in (published, latest, ubuntu)
        )}
        self.prune(["debian12-latest", "ubuntu2404-latest"], {"images": [
            {"key": published["image_key"], "revision": {"sha256": "sha256:" + published["result_sha256"]}},
        ]})
        expected.update({"debian/12/latest.json", "ubuntu/24.04/latest.json"})
        self.assertEqual(set(self.objects), expected)
        for path, build in (("debian/12", latest), ("ubuntu/24.04", ubuntu)):
            pointer = self.objects[path + "/latest.json"]
            self.assertEqual(json.loads(pointer["body"]), {
                "object_key": build["object_key"], "sha256": build["result_sha256"],
                "size_bytes": build["size_bytes"],
            })
            self.assertEqual(pointer["content_type"], "application/json")
            self.assertEqual(pointer["cache_control"], "no-store")

        self.prune([latest["image_key"], ubuntu["image_key"]], {"images": [
            {"key": build["image_key"], "revision": {"sha256": "sha256:" + build["result_sha256"]}}
            for build in (latest, ubuntu)
        ]})
        expected = {key for key in expected if not key.startswith(published["object_key"].rsplit("/", 1)[0] + "/")}
        self.assertEqual(set(self.objects), expected)

    def test_latest_selects_completed_build_and_rejects_missing_or_broken_images(self) -> None:
        image = "debian13-latest"
        output = self.root / "selected.json"
        with self.assertRaisesRegex(ValueError, "no completed build"):
            storage.latest(image, output)
        self.assertFalse(output.exists())
        self.build(image, "1", "2026-09-01T00:00:00Z")
        newest = self.build(image, "2", "2026-09-02T00:00:00Z")
        self.build(image, "3", "2026-09-03T00:00:00Z", attested=False)
        with patch.object(sys, "argv", ["storage.py", "latest", "--image", image, "--output", str(output)]):
            self.assertEqual(storage.main(), 0)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), newest)
        del self.objects[newest["object_key"]]
        with self.assertRaisesRegex(ValueError, "attested build is missing"):
            storage.latest(image, output)

    def test_cleanup_without_catalog_keeps_one_successful_build(self) -> None:
        self.build("debian13-latest", "1", "2026-09-01T00:00:00Z")
        latest = self.build("debian13-latest", "2", "2026-09-02T00:00:00Z")
        self.prune(["debian13-latest"], {"images": []})
        self.assertEqual(len(self.objects), 5)
        self.assertIn(latest["object_key"], self.objects)

    def test_invalid_attested_build_stops_cleanup_before_deletion(self) -> None:
        old = self.build("debian13-latest", "1", "2026-09-01T00:00:00Z")
        latest = self.build("debian13-latest", "2", "2026-09-02T00:00:00Z")
        del self.objects[latest["object_key"]]
        before = dict(self.objects)
        with self.assertRaisesRegex(ValueError, "attested build is missing"):
            self.prune([old["image_key"]], {"images": []})
        self.assertEqual(self.objects, before)

    def test_interrupted_deletion_cannot_leave_a_reusable_build(self) -> None:
        old = self.build("debian13-latest", "1", "2026-09-01T00:00:00Z")
        latest = self.build("debian13-latest", "2", "2026-09-02T00:00:00Z")
        self.failure_key = old["object_key"]
        with self.assertRaisesRegex(OSError, "storage unavailable"):
            self.prune([old["image_key"]], {"images": []})
        builds = storage.attested_builds(old["image_key"], storage.revisions(old["image_key"]))
        self.assertEqual([build["result_sha256"] for build in builds], [latest["result_sha256"]])
        self.failure_key = None
        self.prune([old["image_key"]], {"images": []})
        self.assertEqual(len(self.objects), 5)
        self.assertIn(latest["object_key"], self.objects)

    def test_failed_latest_update_preserves_previous_download_target(self) -> None:
        old = self.build("debian13-latest", "1", "2026-09-01T00:00:00Z")
        self.prune([old["image_key"]], {"images": []})
        latest = self.build("debian13-latest", "2", "2026-09-02T00:00:00Z")
        before = dict(self.objects)
        pointer_key = "debian/13/latest.json"
        self.failure_key = pointer_key
        with self.assertRaisesRegex(OSError, "storage unavailable"):
            self.prune([old["image_key"]], {"images": []})
        self.assertEqual(self.objects, before)
        self.assertEqual(json.loads(self.objects[pointer_key]["body"])["object_key"], old["object_key"])
        self.failure_key = None
        self.prune([old["image_key"]], {"images": []})
        self.assertEqual(json.loads(self.objects[pointer_key]["body"])["object_key"], latest["object_key"])
        self.assertIn(latest["object_key"], self.objects)
        self.assertNotIn(old["object_key"], self.objects)

    def test_reuse_matches_upstream_and_recipe_from_manifest(self) -> None:
        build = self.build("debian13-latest", "1", "2026-09-01T00:00:00Z")
        output = self.root / "cached.json"
        self.assertTrue(storage.reuse(build["image_key"], build["upstream_digest"], output))
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), build)
        self.assertFalse(storage.reuse(build["image_key"], "sha512:" + "ef" * 64, output))
        manifest_key = build["object_key"].rsplit("/", 1)[0] + "/build.json"
        self.objects[manifest_key]["body"] = json.dumps(build | {"recipe_sha256": "ff" * 32}).encode()
        self.assertFalse(storage.reuse(build["image_key"], build["upstream_digest"], output))

    def test_cleanup_stops_when_catalog_is_unavailable_or_tampered(self) -> None:
        fixtures = ROOT / "tests/fixtures"
        public_key = fixtures / "catalog-public.pem"
        self.assertTrue(storage.current_catalog(fixtures / "catalog.json", public_key)["images"])
        value = json.loads((fixtures / "catalog.json").read_text(encoding="utf-8"))
        value["manifest"]["images"][0]["name"] = "Tampered"
        path = self.root / "catalog.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        with patch.object(sys, "argv", ["storage.py", "prune", "--catalog", str(path),
                                        "--public-key", str(public_key)]), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(storage.main(), 1)
        storage.aws.assert_not_called()
        self.assertEqual(storage.current_catalog(None, public_key), {"images": []})
        self.objects["catalog.json"] = {
            "body": (fixtures / "catalog.json").read_bytes(), "modified": "2026-09-05T00:00:00Z",
        }
        self.assertTrue(storage.current_catalog(None, public_key)["images"])
        self.objects["catalog.json"]["body"] = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "signature verification failed"):
            storage.current_catalog(None, public_key)
        with patch.object(storage, "aws", side_effect=subprocess.CalledProcessError(1, "aws")), \
                self.assertRaises(subprocess.CalledProcessError):
            storage.current_catalog(None, public_key)

    def test_fetch_catalog_cli_distinguishes_missing_object_and_storage_failure(self) -> None:
        output = self.root / "current.json"
        content = (ROOT / "tests/fixtures/catalog.json").read_bytes()
        self.objects["catalog.json.backup"] = {
            "body": content, "modified": "2026-09-05T00:00:00Z",
        }
        with patch.object(sys, "argv", ["storage.py", "fetch-catalog", "--output", str(output)]):
            with contextlib.redirect_stdout(io.StringIO()) as result:
                self.assertEqual(storage.main(), 0)
            self.assertEqual(result.getvalue(), "false\n")
            self.assertFalse(output.exists())
            self.objects["catalog.json"] = self.objects["catalog.json.backup"]
            with contextlib.redirect_stdout(io.StringIO()) as result:
                self.assertEqual(storage.main(), 0)
            self.assertEqual(result.getvalue(), "true\n")
            self.assertEqual(output.read_bytes(), content)
            for failing_command in (("s3api", "list-objects-v2"), ("s3", "cp")):
                def fail(*args: str) -> str:
                    if args[:2] == failing_command:
                        raise subprocess.CalledProcessError(1, "aws")
                    return self.aws(*args)
                with self.subTest(command=failing_command), patch.object(storage, "aws", side_effect=fail), \
                        contextlib.redirect_stdout(io.StringIO()) as result, \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(storage.main(), 1)
                self.assertEqual(result.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
