#!/usr/bin/env python3
"""Reuse attested R2 builds, update latest pointers, and remove obsolete revisions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import urlopen

import catalog
import images


CATALOG_URL = "https://github.com/Ithildur/os-images/releases/download/catalog-v1/catalog.json"


def aws(*arguments: str) -> str:
    result = subprocess.run(
        ["aws", "--endpoint-url", os.environ["R2_ENDPOINT_URL"], *arguments],
        check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    return result.stdout


def object_url(key: str) -> str:
    return f"s3://{os.environ['R2_BUCKET']}/{key}"


def revisions(image: str) -> dict:
    path = images.recipe(image)["path"]
    version = path.rsplit("/", 1)[1]
    filenames = {"build.json", f"{version}.qcow2", f"{version}.sha256", f"{version}.provenance.json"}
    listing = json.loads(aws("s3api", "list-objects-v2", "--bucket", os.environ["R2_BUCKET"],
                             "--prefix", path + "/", "--output", "json"))
    result: dict = {}
    for item in listing.get("Contents", []):
        if not item["Key"].startswith(path + "/"):
            continue
        parts = item["Key"][len(path) + 1:].split("/")
        if len(parts) != 2 or not images.valid_digest("sha256", parts[0]) or parts[1] not in filenames:
            continue
        digest, filename = parts
        result.setdefault(digest, {})[filename] = item
    return result


def attested_builds(image: str, versions: dict) -> list[dict]:
    path = images.recipe(image)["path"]
    version = path.rsplit("/", 1)[1]
    result = []
    with tempfile.TemporaryDirectory() as directory:
        manifest = Path(directory) / "build.json"
        for digest, objects in versions.items():
            if "build.json" not in objects:
                continue
            aws("s3", "cp", object_url(objects["build.json"]["Key"]), str(manifest), "--only-show-errors")
            build = catalog.read_build(manifest)
            if build["image_key"] != image or build["object_key"] != f"{path}/{digest}/{version}.qcow2":
                raise ValueError("stored build manifest does not match its object path")
            for filename in (f"{version}.qcow2", f"{version}.sha256", f"{version}.provenance.json"):
                if filename not in objects:
                    raise ValueError(f"attested build is missing {path}/{digest}/{filename}")
            if objects[f"{version}.qcow2"]["Size"] != build["size_bytes"]:
                raise ValueError(f"attested image byte size does not match {build['object_key']}")
            result.append(build)
    return sorted(result, key=lambda build: (
        versions[build["result_sha256"]]["build.json"]["LastModified"], build["result_sha256"],
    ), reverse=True)


def reuse(image: str, upstream_digest: str, output: Path) -> bool:
    recipe_digest = images.recipe_digest(images.recipe(image))
    for build in attested_builds(image, revisions(image)):
        if build["upstream_digest"] == upstream_digest and build["recipe_sha256"] == recipe_digest:
            images.write_json(output, build)
            return True
    return False


def current_catalog(path: Path | None, public_key: Path) -> dict:
    if path is not None:
        value = catalog.read_json(path)
    else:
        try:
            with urlopen(f"{CATALOG_URL}?cleanup={time.time_ns()}", timeout=30) as response:
                value = json.load(response)
        except HTTPError as error:
            if error.code == 404:
                return {"images": []}
            raise
    return catalog.verify_catalog(value, public_key)


def prune(selected: list[str], manifest: dict) -> None:
    published = {image["key"]: image["revision"]["sha256"].removeprefix("sha256:")
                 for image in manifest["images"]}
    deletions = []
    latest = []
    for image in selected:
        versions = revisions(image)
        builds = attested_builds(image, versions)
        keep = {published[image]} if image in published else set()
        if builds:
            keep.add(builds[0]["result_sha256"])
            latest.append(builds[0])
        for digest, objects in versions.items():
            if digest not in keep:
                # Remove the completion marker before removing any referenced bytes.
                deletions.extend(item["Key"] for name, item in sorted(
                    objects.items(), key=lambda pair: (pair[0] != "build.json", pair[0]),
                ))
    # Update pointers before deleting any version that an old pointer may reference.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "latest.json"
        for build in latest:
            key = images.recipe(build["image_key"])["path"] + "/latest.json"
            images.write_json(path, {
                "object_key": build["object_key"], "sha256": build["result_sha256"],
                "size_bytes": build["size_bytes"],
            })
            aws("s3", "cp", str(path), object_url(key), "--content-type", "application/json",
                "--cache-control", "no-store", "--only-show-errors")
            print(f"Updated {key}")
    for key in deletions:
        aws("s3", "rm", object_url(key), "--only-show-errors")
        print(f"Deleted {key}")
    print(f"Removed {len(deletions)} obsolete image objects.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cached = commands.add_parser("reuse")
    cached.add_argument("--image", required=True)
    cached.add_argument("--upstream-digest", required=True)
    cached.add_argument("--output", type=Path, required=True)
    cleanup = commands.add_parser("prune")
    cleanup.add_argument("--image", default="all")
    cleanup.add_argument("--catalog", type=Path)
    cleanup.add_argument("--public-key", type=Path, default=images.ROOT / "catalog/keys/catalog-2026-01.pem")
    arguments = parser.parse_args()
    try:
        if arguments.command == "reuse":
            print(str(reuse(arguments.image, arguments.upstream_digest, arguments.output)).lower())
        else:
            selected = list(images.recipes()) if arguments.image == "all" else [arguments.image]
            manifest = current_catalog(arguments.catalog, arguments.public_key)
            prune(selected, manifest)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"storage: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
