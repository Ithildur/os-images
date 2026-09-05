#!/usr/bin/env python3
"""Image recipes, upstream checksums, and build evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


def recipes() -> dict:
    entries = json.loads((ROOT / "recipes/images.json").read_text(encoding="utf-8"))
    paths = set()
    for key, entry in entries.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key):
            raise ValueError(f"invalid image key: {key}")
        if not re.fullmatch(r"[a-z0-9-]+", entry["family"]) or not (ROOT / "recipes" / entry["family"] / "customize.sh").is_file():
            raise ValueError(f"missing family recipe for {key}")
        if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9.-]+", entry["path"]) or entry["path"] in paths:
            raise ValueError(f"invalid or duplicate object path for {key}")
        if entry["checksum_algorithm"] not in {"sha256", "sha512"}:
            raise ValueError(f"unsupported upstream checksum for {key}")
        for field in ("url", "checksums", "license"):
            if urlsplit(entry[field]).scheme != "https" or not urlsplit(entry[field]).hostname:
                raise ValueError(f"invalid {field} for {key}")
        paths.add(entry["path"])
    return entries


def recipe(key: str) -> dict:
    entries = recipes()
    if key not in entries:
        raise ValueError(f"unknown image key: {key}")
    return entries[key]


def valid_digest(algorithm: str, digest: object) -> bool:
    length = {"sha256": 64, "sha512": 128}[algorithm]
    return isinstance(digest, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", digest) is not None


def checksum(text: str, filename: str, algorithm: str) -> str:
    matches = []
    tagged = f"{algorithm.upper()} ({filename}) = "
    for line in text.splitlines():
        if line.startswith(tagged):
            matches.append(line.removeprefix(tagged).strip())
        else:
            parts = line.split()
            if len(parts) == 2 and parts[1].removeprefix("*") == filename:
                matches.append(parts[0])
    if len(matches) != 1 or not valid_digest(algorithm, matches[0]):
        raise ValueError(f"official {algorithm} entry for {filename} is missing, invalid, or ambiguous")
    return matches[0]


def recipe_digest(entry: dict) -> str:
    digest = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    files = [ROOT / "scripts/build-image.sh", ROOT / "recipes" / entry["family"] / "customize.sh"]
    files.extend(sorted((ROOT / "recipes/linux").iterdir()))
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def locate(object_key: str) -> str:
    try:
        path, digest, filename = object_key.rsplit("/", 2)
    except ValueError as error:
        raise ValueError("image object key is invalid") from error
    for key, entry in recipes().items():
        if path == entry["path"]:
            if valid_digest("sha256", digest) and filename == path.rsplit("/", 1)[1] + ".qcow2":
                return key
            break
    raise ValueError("image object key does not match a registered image and its content digests")


def resolve(key: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("upstream resolution is remote-only")
    entry = recipe(key)
    filename = urlsplit(entry["url"]).path.rsplit("/", 1)[1]
    with urlopen(entry["checksums"], timeout=30) as response:
        text = response.read().decode("utf-8")
    digest = checksum(text, filename, entry["checksum_algorithm"])
    print(f"upstream_url={entry['url']}")
    print(f"upstream_filename={filename}")
    print(f"checksum_algorithm={entry['checksum_algorithm']}")
    print(f"upstream_digest={entry['checksum_algorithm']}:{digest}")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record(arguments: argparse.Namespace) -> None:
    entry = recipe(arguments.image)
    algorithm, upstream = arguments.upstream_digest.split(":", 1)
    if algorithm != entry["checksum_algorithm"] or not valid_digest(algorithm, upstream):
        raise ValueError("upstream digest does not match the image recipe")
    if not valid_digest("sha256", arguments.sha256) or not 1 <= arguments.size_bytes <= 2**63 - 1:
        raise ValueError("image digest or byte size is invalid")
    recipe_hash = recipe_digest(entry)
    filename = entry["path"].rsplit("/", 1)[1] + ".qcow2"
    object_key = f"{entry['path']}/{arguments.sha256}/{filename}"
    build = {
        "schema": "vps-manager-image-build/v1", "image_key": arguments.image,
        "image_name": entry["name"], "upstream_digest": arguments.upstream_digest,
        "recipe_sha256": recipe_hash, "result_sha256": arguments.sha256,
        "size_bytes": arguments.size_bytes, "format": "qcow2", "object_key": object_key,
        "provenance_key": object_key.removesuffix(".qcow2") + ".provenance.json",
        "contract": json.loads((ROOT / "recipes/linux/contract.json").read_text(encoding="utf-8")),
    }
    packages = []
    for line in (arguments.metadata / "packages.tsv").read_text(encoding="utf-8").splitlines():
        name, version = line.split("\t", 1)
        packages.append({"name": name, "version": version})
    provenance = {
        "schema": "vps-manager-image-provenance/v1", "image_key": arguments.image,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"url": entry["url"], algorithm: upstream}, "recipe_sha256": recipe_hash,
        "workflow": {"repository": os.environ["GITHUB_REPOSITORY"], "commit": os.environ["GITHUB_SHA"],
                     "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"]},
        "packages": packages,
        "customization": ["root cloud-init account", "SSH password authentication", "serial console",
                          "root filesystem growth", "qemu-guest-agent", "instance identity cleanup"],
        "license": entry["license"],
    }
    write_json(arguments.output / "build.json", build)
    write_json(arguments.output / "provenance.json", provenance)
    (arguments.output / "image.sha256").write_text(f"{arguments.sha256}  {filename}\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    matrix = commands.add_parser("matrix")
    matrix.add_argument("image", nargs="?", default="all")
    for command in ("family", "resolve"):
        commands.add_parser(command).add_argument("image")
    commands.add_parser("locate").add_argument("object_key")
    build = commands.add_parser("record")
    build.add_argument("--image", required=True)
    build.add_argument("--upstream-digest", required=True)
    build.add_argument("--sha256", required=True)
    build.add_argument("--size-bytes", type=int, required=True)
    build.add_argument("--metadata", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        match arguments.command:
            case "matrix":
                entries = recipes()
                if arguments.image != "all":
                    recipe(arguments.image)
                print(json.dumps(list(entries) if arguments.image == "all" else [arguments.image]))
            case "family":
                print(recipe(arguments.image)["family"])
            case "resolve":
                resolve(arguments.image)
            case "locate":
                locate(arguments.object_key)
                print(arguments.object_key.rsplit("/", 1)[0] + "/build.json")
            case "record":
                record(arguments)
    except (ValueError, OSError, KeyError) as error:
        print(f"images: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
