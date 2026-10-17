from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import images


class ImageRecipeTest(unittest.TestCase):
    def test_resolve_retries_transient_failures_before_emitting_outputs(self) -> None:
        entry = images.recipe("centos9-stream-latest")
        filename = entry["url"].rsplit("/", 1)[1]
        digest = "ab" * 32
        errors = [HTTPError(entry["checksums"], code, "temporary failure", {}, None)
                  for code in (408, 429, 500, 502, 503, 504)]
        errors.extend([URLError("connection failed"), TimeoutError("timed out"),
                       ConnectionResetError("connection reset")])
        for error in errors:
            with self.subTest(error=error), patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                    patch.object(images, "urlopen", side_effect=[
                        error, io.BytesIO(f"{digest}  {filename}\n".encode()),
                    ]) as request, patch.object(images.time, "sleep") as sleep, \
                    contextlib.redirect_stdout(io.StringIO()) as output, \
                    contextlib.redirect_stderr(io.StringIO()) as diagnostics:
                images.resolve("centos9-stream-latest")
                self.assertEqual(output.getvalue(),
                                 f"upstream_url={entry['url']}\nupstream_filename={filename}\n"
                                 f"checksum_algorithm=sha256\nupstream_digest=sha256:{digest}\n")
                self.assertEqual(request.call_count, 2)
                sleep.assert_called_once_with(2)
                self.assertIn("retrying", diagnostics.getvalue())

    def test_resolve_stops_after_three_failed_requests(self) -> None:
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                patch.object(images, "urlopen", side_effect=HTTPError(
                    "https://example.test/checksums", 502, "Bad Gateway", {}, None,
                )) as request, patch.object(images.time, "sleep") as sleep, \
                patch.object(sys, "argv", ["images.py", "resolve", "centos9-stream-latest"]), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as diagnostics:
            self.assertEqual(images.main(), 1)
            self.assertEqual(request.call_count, 3)
            self.assertEqual(sleep.call_args_list, [call(2), call(4)])
            self.assertEqual(output.getvalue(), "")
            self.assertIn("HTTP Error 502: Bad Gateway", diagnostics.getvalue())

    def test_resolve_does_not_retry_rejections_or_invalid_checksums(self) -> None:
        for failure in (401, 403, 404, "invalid checksum"):
            with self.subTest(failure=failure), patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                    patch.object(images, "urlopen") as request, \
                    patch.object(images.time, "sleep") as sleep, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                if isinstance(failure, int):
                    request.side_effect = HTTPError("https://example.test/checksums", failure,
                                                   "request rejected", {}, None)
                    expected = HTTPError
                else:
                    request.return_value = io.BytesIO(b"invalid checksum\n")
                    expected = ValueError
                with self.assertRaises(expected):
                    images.resolve("centos9-stream-latest")
                request.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(output.getvalue(), "")

    def test_record_emits_version_filename_and_verifiable_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "fixture/os-images", "GITHUB_SHA": "fixture",
            "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
        }):
            output = Path(directory)
            (output / "packages.tsv").write_text("cloud-init\t1\n", encoding="utf-8")
            content = b"image fixture\n"
            digest = hashlib.sha256(content).hexdigest()
            for key, entry in images.recipes().items():
                with self.subTest(image=key):
                    algorithm = entry["checksum_algorithm"]
                    upstream = "ab" * (64 if algorithm == "sha512" else 32)
                    images.record(argparse.Namespace(image=key, upstream_digest=f"{algorithm}:{upstream}",
                                                     sha256=digest, size_bytes=len(content), metadata=output, output=output))
                    build = json.loads((output / "build.json").read_text(encoding="utf-8"))
                    version = entry["path"].rsplit("/", 1)[1]
                    filename = f"{version}.qcow2"
                    self.assertEqual(build["object_key"], f"{entry['path']}/{digest}/{filename}")
                    (output / filename).write_bytes(content)
                    subprocess.run(["sha256sum", "--check", "image.sha256"], cwd=output,
                                   check=True, capture_output=True)

    def test_checksum_selects_exact_image_and_rejects_ambiguity(self) -> None:
        for algorithm, digest in (("sha256", "ab" * 32), ("sha512", "cd" * 64)):
            with self.subTest(algorithm=algorithm):
                sums = f"{digest}  server.img.manifest\n{digest} *server.img\n"
                tagged = f"# server.img: 4096 bytes\n{algorithm.upper()} (server.img) = {digest}\n"
                self.assertEqual(images.checksum(sums, "server.img", algorithm), digest)
                self.assertEqual(images.checksum(tagged, "server.img", algorithm), digest)
                for invalid in ("", sums + sums, sums + tagged, "invalid  server.img\n"):
                    with self.assertRaises(ValueError):
                        images.checksum(invalid, "server.img", algorithm)

    def test_registry_extension_drives_matrix_and_object_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "recipes", root / "recipes")
            shutil.copytree(ROOT / "scripts", root / "scripts")
            registry = root / "recipes/images.json"
            entries = json.loads(registry.read_text(encoding="utf-8"))
            entries["downstream-latest"] = entries["debian13-latest"] | {
                "name": "Downstream Linux", "path": "downstream/1",
            }
            registry.write_text(json.dumps(entries), encoding="utf-8")
            result = subprocess.run([sys.executable, root / "scripts/images.py", "matrix"],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), list(entries))
            object_key = f"downstream/1/{'ef' * 32}/1.qcow2"
            result = subprocess.run([sys.executable, root / "scripts/images.py", "locate", object_key],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), object_key.rsplit("/", 1)[0] + "/build.json")

    def test_recipe_changes_invalidate_build_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "recipes", root / "recipes")
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts/build-image.sh", root / "scripts/build-image.sh")
            with patch.object(images, "ROOT", root):
                entry = images.recipe("debian13-latest")
                original = images.recipe_digest(entry)
                changed_source = copy.deepcopy(entry)
                changed_source["url"] = "https://images.example.test/new.qcow2"
                self.assertNotEqual(images.recipe_digest(changed_source), original)
                config = root / "recipes/linux/cloud.cfg"
                config.write_text(config.read_text(encoding="utf-8") + "manage_etc_hosts: true\n", encoding="utf-8")
                changed_config = images.recipe_digest(entry)
                self.assertNotEqual(changed_config, original)
                family = root / "recipes/debian/customize.sh"
                family.write_text(family.read_text(encoding="utf-8") + "apt-get autoremove --yes\n", encoding="utf-8")
                self.assertNotEqual(images.recipe_digest(entry), changed_config)


if __name__ == "__main__":
    unittest.main()
