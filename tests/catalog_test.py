from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CatalogToolTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.private_key = self.root / "private.pem"
        self.public_key = self.root / "public.pem"
        self.catalog = self.root / "catalog.json"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", self.private_key], check=True)
        subprocess.run(["openssl", "pkey", "-in", self.private_key, "-pubout", "-out", self.public_key], check=True)

    def test_panel_catalog_fixture(self) -> None:
        fixtures = ROOT / "tests/fixtures"
        self.run_tool("catalog", "verify", fixtures / "catalog.json", "--public-key", fixtures / "catalog-public.pem")

    def test_signed_catalog_detects_tamper(self) -> None:
        build = self.build("debian13-latest")
        self.publish(build)
        self.run_tool("catalog", "verify", self.catalog, "--public-key", self.public_key)
        value = json.loads(self.catalog.read_text(encoding="utf-8"))
        value["manifest"]["images"][0]["name"] = "Tampered"
        self.catalog.write_text(json.dumps(value), encoding="utf-8")
        result = self.run_tool("catalog", "verify", self.catalog, "--public-key", self.public_key, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_publish_multiple_images_preserves_other_revisions(self) -> None:
        entries = json.loads((ROOT / "recipes/images.json").read_text(encoding="utf-8"))
        for key in entries:
            self.publish(self.build(key))
        value = json.loads(self.catalog.read_text(encoding="utf-8"))
        self.assertEqual(value["manifest"]["sequence"], len(entries))
        by_key = {image["key"]: image for image in value["manifest"]["images"]}
        self.assertEqual(set(by_key), set(entries))
        for key, entry in entries.items():
            build = json.loads((self.root / key / "build.json").read_text(encoding="utf-8"))
            self.assertEqual(by_key[key], {
                "key": key, "name": entry["name"], "kind": "bootable",
                "revision": {"url": f"https://images.example.test/{build['object_key']}",
                             "sha256": "sha256:" + build["result_sha256"], "size_bytes": build["size_bytes"],
                             "format": build["format"], "contract": build["contract"]},
            })

        updated = self.build("debian12-latest", "ef" * 32)
        self.publish(updated)
        next_catalog = json.loads(self.catalog.read_text(encoding="utf-8"))
        self.assertEqual(next_catalog["manifest"]["sequence"], len(entries) + 1)
        for image in next_catalog["manifest"]["images"]:
            if image["key"] == "debian12-latest":
                self.assertEqual(image["revision"]["sha256"], "sha256:" + "ef" * 32)
            else:
                self.assertEqual(image, by_key[image["key"]])
        self.publish(updated)
        self.assertEqual(json.loads(self.catalog.read_text(encoding="utf-8")), next_catalog)

    def test_published_revision_url_is_immutable(self) -> None:
        build = self.build("ubuntu2404-latest")
        self.publish(build)
        result = self.publish(build, url="https://mirror.example.test/image.qcow2", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("published image revision is immutable", result.stderr)

    def test_build_manifest_binds_image_identity_and_digests(self) -> None:
        build_path = self.build("ubuntu2204-latest")
        original = json.loads(build_path.read_text(encoding="utf-8"))
        for field, invalid in (("image_key", "debian13-latest"), ("object_key", "ubuntu/22.04/wrong.qcow2"),
                               ("result_sha256", "ef" * 32), ("recipe_sha256", "invalid")):
            with self.subTest(field=field):
                build_path.write_text(json.dumps(original | {field: invalid}), encoding="utf-8")
                result = self.publish(build_path, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("catalog:", result.stderr)

    def build(self, key: str, digest: str = "ab" * 32) -> Path:
        output = self.root / key
        output.mkdir(exist_ok=True)
        (output / "packages.tsv").write_text("cloud-init\t1.0\nqemu-guest-agent\t1.0\n", encoding="utf-8")
        entry = json.loads((ROOT / "recipes/images.json").read_text(encoding="utf-8"))[key]
        algorithm = entry["checksum_algorithm"]
        upstream = "cd" * (64 if algorithm == "sha512" else 32)
        self.run_tool("images", "record", "--image", key, "--upstream-digest", f"{algorithm}:{upstream}",
                      "--sha256", digest, "--size-bytes", "4096", "--metadata", output, "--output", output)
        return output / "build.json"

    def publish(self, build: Path, *, url: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        manifest = json.loads(build.read_text(encoding="utf-8"))
        current = ["--current", self.catalog] if self.catalog.exists() else []
        return self.run_tool("catalog", "publish", *current, "--build", build,
                             "--url", url or f"https://images.example.test/{manifest['object_key']}",
                             "--private-key", self.private_key, "--public-key", self.public_key,
                             "--output", self.catalog, check=check)

    @staticmethod
    def run_tool(tool: str, *arguments: object, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / f"{tool}.py"), *(str(argument) for argument in arguments)],
            check=check, text=True, capture_output=True,
            env=os.environ | {"GITHUB_REPOSITORY": "fixture/os-images", "GITHUB_SHA": "fixture",
                              "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1"},
        )


if __name__ == "__main__":
    unittest.main()
