#!/usr/bin/env python3
"""Build and verify the detached-signature VPS Manager image Catalog."""

from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from images import locate, recipe, valid_digest


SCHEMA = "vps-manager-image-catalog/v1"
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
ALLOWED_IMAGE_KEYS = {"key", "name", "kind", "revision"}
ALLOWED_REVISION_KEYS = {"url", "sha256", "size_bytes", "format", "contract"}


class CatalogError(ValueError):
    pass


def exact_keys(value: dict, expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CatalogError(f"{label} fields are invalid")


def canonical(value: object) -> bytes:
    # Catalog v1 uses Go encoding/json string escaping at the Panel boundary.
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for character in ("&", "<", ">", "\u2028", "\u2029"):
        encoded = encoded.replace(character, f"\\u{ord(character):04x}")
    return encoded.encode("utf-8")


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CatalogError(f"cannot read JSON from {path}: {error}") from error


def valid_time(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def validate_url(value: object) -> None:
    if not isinstance(value, str) or len(value) > 2048:
        raise CatalogError("revision URL is invalid")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise CatalogError("revision URL must be a public HTTPS URL")


def validate_contract(contract: object, image_kind: str, image_format: str) -> None:
    if not isinstance(contract, dict):
        raise CatalogError("image contract is invalid")
    allowed = {"purpose", "guest", "expires_at"}
    if not set(contract).issubset(allowed) or "purpose" not in contract:
        raise CatalogError("image contract fields are invalid")
    purpose = contract["purpose"]
    if purpose not in {"provisioning", "install_media", "rescue"}:
        raise CatalogError("image purpose is invalid")
    guest = contract.get("guest")
    expiry = contract.get("expires_at")
    if purpose == "install_media":
        if image_kind != "install_media" or image_format != "iso" or guest is not None or expiry is not None:
            raise CatalogError("installation media contract is invalid")
        return
    if image_kind != "bootable" or image_format not in {"qcow2", "raw"} or not isinstance(guest, dict):
        raise CatalogError("bootable image contract is invalid")
    if purpose == "provisioning" and expiry is not None or purpose == "rescue" and not valid_time(expiry):
        raise CatalogError("image contract expiry is invalid")
    required = {
        "family", "architecture", "credential_user", "initialization", "generalized",
        "guest_agent", "firmware", "signed_virtio_drivers",
    }
    if not required.issubset(guest) or not set(guest).issubset(required | {"hyper_v_features"}):
        raise CatalogError("guest contract fields are invalid")
    if guest["family"] not in {"linux", "windows"} or guest["architecture"] not in {"x86_64", "aarch64"}:
        raise CatalogError("guest family or architecture is invalid")
    if not isinstance(guest["credential_user"], str) or not guest["credential_user"] or len(guest["credential_user"]) > 128:
        raise CatalogError("guest credential user is invalid")
    if guest["generalized"] is not True or not isinstance(guest["guest_agent"], bool) or not isinstance(guest["signed_virtio_drivers"], bool):
        raise CatalogError("guest capability facts are invalid")
    initialization = guest["initialization"]
    if not isinstance(initialization, dict) or set(initialization) != {"kind", "data_source"}:
        raise CatalogError("guest initialization is invalid")
    expected_init = ("linux_cloud_init", "nocloud") if guest["family"] == "linux" else ("windows_config_drive", "config_drive_v2")
    if (initialization["kind"], initialization["data_source"]) != expected_init:
        raise CatalogError("guest initialization does not match its family")
    firmware = guest["firmware"]
    if not isinstance(firmware, dict) or not {"type", "secure_boot"}.issubset(firmware) or not set(firmware).issubset({"type", "secure_boot", "tpm_model", "tpm_version"}):
        raise CatalogError("guest firmware contract is invalid")
    if firmware["type"] not in {"bios", "uefi"} or not isinstance(firmware["secure_boot"], bool):
        raise CatalogError("guest firmware facts are invalid")
    features = guest.get("hyper_v_features", [])
    if not isinstance(features, list) or any(not isinstance(item, str) for item in features) or features != sorted(set(features)):
        raise CatalogError("Hyper-V feature list is invalid")


def validate_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict):
        raise CatalogError("catalog manifest is invalid")
    exact_keys(manifest, {"sequence", "images"}, "manifest")
    sequence = manifest["sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or not 1 <= sequence <= 2**63 - 1:
        raise CatalogError("catalog sequence is invalid")
    images = manifest["images"]
    if not isinstance(images, list) or not images:
        raise CatalogError("catalog image list is invalid")
    keys: set[str] = set()
    names: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            raise CatalogError("catalog image is invalid")
        exact_keys(image, ALLOWED_IMAGE_KEYS, "image")
        key, name, kind = image["key"], image["name"], image["kind"]
        if not isinstance(key, str) or not KEY.fullmatch(key) or key in keys:
            raise CatalogError("catalog image key is invalid or duplicated")
        if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 128 or name in names:
            raise CatalogError("catalog image name is invalid or duplicated")
        if kind not in {"bootable", "install_media"}:
            raise CatalogError("catalog image kind is invalid")
        revision = image["revision"]
        if not isinstance(revision, dict):
            raise CatalogError("catalog revision is invalid")
        exact_keys(revision, ALLOWED_REVISION_KEYS, "revision")
        validate_url(revision["url"])
        if not isinstance(revision["sha256"], str) or not DIGEST.fullmatch(revision["sha256"]):
            raise CatalogError("revision SHA-256 is invalid")
        if not isinstance(revision["size_bytes"], int) or isinstance(revision["size_bytes"], bool) or not 1 <= revision["size_bytes"] <= 2**63 - 1:
            raise CatalogError("revision size is invalid")
        if revision["format"] not in {"qcow2", "raw", "iso"}:
            raise CatalogError("revision format is invalid")
        validate_contract(revision["contract"], kind, revision["format"])
        keys.add(key)
        names.add(name)
    return manifest


def validate_catalog(value: object) -> dict:
    if not isinstance(value, dict):
        raise CatalogError("catalog is invalid")
    exact_keys(value, {"schema", "manifest", "signature"}, "catalog")
    if value["schema"] != SCHEMA:
        raise CatalogError("catalog schema is unsupported")
    manifest = validate_manifest(value["manifest"])
    signature = value["signature"]
    if not isinstance(signature, str):
        raise CatalogError("catalog signature is invalid")
    try:
        raw = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error, TypeError) as error:
        raise CatalogError("catalog signature encoding is invalid") from error
    if len(raw) != 64:
        raise CatalogError("catalog signature length is invalid")
    return manifest


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify_catalog(catalog: object, public_key: Path) -> dict:
    manifest = validate_catalog(catalog)
    signature = base64.b64decode(catalog["signature"], validate=True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        message = root / "manifest.json"
        raw_signature = root / "manifest.sig"
        message.write_bytes(canonical(manifest))
        raw_signature.write_bytes(signature)
        result = subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key), "-rawin", "-in", str(message), "-sigfile", str(raw_signature)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if result.returncode != 0:
        raise CatalogError("catalog signature verification failed")
    return manifest


def sign_catalog(manifest: dict, private_key: Path) -> dict:
    validate_manifest(manifest)
    message = canonical(manifest)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        canonical_path = root / "manifest.json"
        signature_path = root / "manifest.sig"
        canonical_path.write_bytes(message)
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(private_key), "-rawin", "-in", str(canonical_path), "-out", str(signature_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        signature = signature_path.read_bytes()
    if len(signature) != 64:
        raise CatalogError("OpenSSL returned an invalid Ed25519 signature")
    catalog = {
        "schema": SCHEMA,
        "manifest": manifest,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    validate_catalog(catalog)
    return catalog


def read_build(path: Path) -> dict:
    build = read_json(path)
    if not isinstance(build, dict):
        raise CatalogError("build manifest is invalid")
    exact_keys(build, {
        "schema", "image_key", "image_name", "upstream_digest", "recipe_sha256", "result_sha256", "size_bytes",
        "format", "object_key", "provenance_key", "contract",
    }, "build manifest")
    if build["schema"] != "vps-manager-image-build/v1" or not isinstance(build["image_key"], str):
        raise CatalogError("build manifest schema or image key is invalid")
    entry = recipe(build["image_key"])
    if not isinstance(build["upstream_digest"], str) or ":" not in build["upstream_digest"]:
        raise CatalogError("build upstream digest is invalid")
    algorithm, upstream = build["upstream_digest"].split(":", 1)
    if algorithm != entry["checksum_algorithm"] or not valid_digest(algorithm, upstream):
        raise CatalogError("build upstream digest does not match the image recipe")
    if not valid_digest("sha256", build["recipe_sha256"]):
        raise CatalogError("build recipe SHA-256 is invalid")
    if not isinstance(build["image_name"], str) or not build["image_name"].strip() or build["image_name"] != build["image_name"].strip():
        raise CatalogError("build image name is invalid")
    if not isinstance(build["result_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", build["result_sha256"]):
        raise CatalogError("build SHA-256 is invalid")
    size = build["size_bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 2**63 - 1:
        raise CatalogError("build size is invalid")
    filename = entry["path"].rsplit("/", 1)[1] + ".qcow2"
    object_key = f"{entry['path']}/{build['result_sha256']}/{filename}"
    if build["format"] != "qcow2" or build["object_key"] != object_key or build["provenance_key"] != object_key.removesuffix(".qcow2") + ".provenance.json":
        raise CatalogError("build object keys or format do not match its content digests")
    validate_contract(build["contract"], "bootable", build["format"])
    return build


def command_verify(arguments: argparse.Namespace) -> None:
    verify_catalog(read_json(arguments.catalog), arguments.public_key)


def command_build(arguments: argparse.Namespace) -> None:
    build = read_build(arguments.manifest)
    if build["object_key"] != arguments.object_key or build["image_key"] != locate(arguments.object_key):
        raise CatalogError("build manifest does not reference the requested image object")
    print(f"sha256={build['result_sha256']}")
    print(f"size_bytes={build['size_bytes']}")
    print(f"object_key={build['object_key']}")


def command_publish(arguments: argparse.Namespace) -> None:
    current: object | None = None
    images: list[dict] = []
    sequence = 1
    if arguments.current is not None:
        current = read_json(arguments.current)
        manifest = verify_catalog(current, arguments.public_key)
        images = list(manifest["images"])
        sequence = manifest["sequence"] + 1

    build = read_build(arguments.build)
    revision = {
        "url": arguments.url,
        "sha256": f"sha256:{build['result_sha256']}",
        "size_bytes": build["size_bytes"],
        "format": build["format"],
        "contract": build["contract"],
    }
    validate_url(revision["url"])
    expected_digest = revision["sha256"]
    for image in images:
        if image["key"] == build["image_key"] and image["revision"]["sha256"] == expected_digest:
            if image["revision"] != revision:
                raise CatalogError("published image revision is immutable; URL and build facts must match")
            write_json(arguments.output, current)
            return

    replacement = {
        "key": build["image_key"],
        "name": build["image_name"],
        "kind": "bootable",
        "revision": revision,
    }
    replaced = False
    next_images: list[dict] = []
    for image in images:
        if image["key"] == replacement["key"]:
            next_images.append(replacement)
            replaced = True
        else:
            next_images.append(image)
    if not replaced:
        next_images.append(replacement)

    catalog = sign_catalog({"sequence": sequence, "images": next_images}, arguments.private_key)
    verify_catalog(catalog, arguments.public_key)
    write_json(arguments.output, catalog)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("catalog", type=Path)
    verify.add_argument("--public-key", type=Path, required=True)
    verify.set_defaults(run=command_verify)
    build = commands.add_parser("build")
    build.add_argument("manifest", type=Path)
    build.add_argument("--object-key", required=True)
    build.set_defaults(run=command_build)
    publish = commands.add_parser("publish")
    publish.add_argument("--current", type=Path)
    publish.add_argument("--public-key", type=Path, required=True)
    publish.add_argument("--private-key", type=Path, required=True)
    publish.add_argument("--url", required=True)
    publish.add_argument("--build", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    publish.set_defaults(run=command_publish)
    return root


def main() -> int:
    arguments = parser().parse_args()
    try:
        arguments.run(arguments)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"catalog: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
