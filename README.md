# VPS Manager built-in images

This repository publishes neutral operating-system images consumed by both the
PVE and Native QEMU/KVM providers. Image bytes are stored outside Git.

## Image recipes

`recipes/images.json` defines the image keys, names, upstream URLs, checksums,
object paths, and distribution families. All listed images target x86_64 KVM
servers, use BIOS boot and QCOW2 disks, and have no desktop environment.

| Image key | System | Official upstream | Checksum | Family recipe |
| --- | --- | --- | --- | --- |
| `debian11-latest` | Debian 11 | [bullseye generic](https://cloud.debian.org/images/cloud/bullseye/latest/) | SHA-512 | `debian` |
| `debian12-latest` | Debian 12 | [bookworm generic](https://cloud.debian.org/images/cloud/bookworm/latest/) | SHA-512 | `debian` |
| `debian13-latest` | Debian 13 | [trixie generic](https://cloud.debian.org/images/cloud/trixie/latest/) | SHA-512 | `debian` |
| `ubuntu2204-latest` | Ubuntu 22.04 LTS | [Jammy Server Cloud](https://cloud-images.ubuntu.com/releases/jammy/release/) | SHA-256 | `debian` |
| `ubuntu2404-latest` | Ubuntu 24.04 LTS | [Noble Server Cloud](https://cloud-images.ubuntu.com/releases/noble/release/) | SHA-256 | `debian` |
| `centos9-stream-latest` | CentOS Stream 9 | [CentOS GenericCloud](https://cloud.centos.org/centos/9-stream/x86_64/images/) | SHA-256 | `redhat` |

The build verifies the selected image's exact entry in the official checksum
file before customization. Debian and Ubuntu share `recipes/debian/customize.sh`.
It installs `cloud-init`, `cloud-guest-utils`, `qemu-guest-agent`, and
`openssh-server` with `--no-install-recommends`.
CentOS Stream uses `recipes/redhat/customize.sh`, with DNF, `cloud-utils-growpart`,
GRUB/BLS configuration through `grubby`, and SELinux label restoration.

`recipes/linux/` holds the shared initialization contract and configuration:
root initialization through NoCloud, SSH password authentication, serial
console, and automatic root-partition and filesystem growth. Both family recipes
run the shared `recipes/linux/configure.sh` to apply initialization, SSH,
Guest Agent, serial-getty, and identity cleanup. Package installation, boot-loader
updates, and package-cache cleanup belong to the family recipe.
The build clears cloud-init state, machine identity, SSH host keys,
temporary files, generated network configuration, and package caches. It does
not contain a password, DNS server, IP address, hostname, Provider setting, or
PVE template identity, and it does not disable operating-system update
services.

The provenance next to each image records its upstream URL and digest,
license URL, workflow run, source commit, installed package versions, recipe
digest, and customization set.

### Adding an image

Add an entry to `recipes/images.json` with a unique image key and object path.
Use `debian` or `redhat` for a compatible derivative. The build matrix,
object lookup, and Catalog publisher read this registry directly.

A distribution family supplies `recipes/<family>/customize.sh`, which consumes
the shared files copied to `/var/lib/vps-manager-image/linux`, runs its shared
`configure.sh`, and writes installed packages as `name<TAB>version` rows to
`/var/lib/vps-manager-image/packages.tsv`. The build removes this directory before
publishing the image. Each new image needs its own release acceptance.

## Published objects

Each revision uses the final image's SHA-256 as its immutable R2 directory:

```text
<os>/<os_version>/<sha256>/<os_version>.qcow2
<os>/<os_version>/<sha256>/<os_version>.sha256
<os>/<os_version>/<sha256>/<os_version>.provenance.json
<os>/<os_version>/<sha256>/build.json
```

For example, Debian 12 uses `debian/12/<sha256>/12.qcow2` and `12.sha256`.
The checksum file contains `<sha256>  <os_version>.qcow2` and can be checked with
`sha256sum --check <os_version>.sha256` from the downloaded image's directory.

Each system/version also has a fixed `<os>/<os_version>/latest.json` object.
For example, `debian/12/latest.json` contains:

```json
{
  "object_key": "debian/12/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/12.qcow2",
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "size_bytes": 4096
}
```

`object_key` is relative to the bucket root. Append it to your public download
base URL to fetch the image, then verify its SHA-256 and byte size.
The SHA-256 is lowercase hexadecimal without an algorithm prefix.
`latest.json` identifies the most recent successful build or reuse. It is
uploaded with `Content-Type: application/json` and `Cache-Control: no-store`.
The signed Catalog identifies the published revisions.

The signed runtime Catalog is published at
<https://github.com/Ithildur/os-images/releases/download/catalog-v1/catalog.json>.
It contains only runtime identity, download, digest, size, format, and image
contract fields. Build manifests and provenance remain separate from the
runtime Catalog.

Catalog v1 encodes `sequence` and `size_bytes` as positive JSON integers that
fit PostgreSQL `BIGINT`. Its signed manifest uses UTF-8 JSON, sorted object
keys, no insignificant whitespace, and Go `encoding/json` string escaping
(including `&`, `<`, `>`, U+2028, and U+2029). Publication starts at sequence 1
only when the Catalog URL returns HTTP 404; other download failures stop publication.
The build manifest records the upstream digest and recipe digest. The recipe
digest covers the image entry, build script, family script, and shared Linux
configuration. An attested build is reused only for identical upstream and
recipe digests, with all referenced objects present.

The workflows list objects under registered system/version prefixes. R2
credentials require [Object Read & Write permissions](https://developers.cloudflare.com/r2/api/tokens/),
including listing, uploading, reading, and deleting objects.
A build becomes reusable only after its artifact attestation succeeds;
R2 access failures stop the workflow.

## Build and publish

All workflow jobs run as root in `debian:13-slim` containers. GitHub's Ubuntu
runner hosts Docker; workflow tools come from Debian. The image build container
uses `/dev/kvm` and installs a Debian kernel and modules for the libguestfs
appliance. It uses the direct backend and QEMU's SLIRP networking.

Configure these secrets in the GitHub Actions environment `R2`
(repository **Settings → Environments → R2 → Environment secrets**):

| Type | Name | Value |
| --- | --- | --- |
| Secret | `R2_ACCESS_KEY_ID` | R2 S3 access key ID |
| Secret | `R2_SECRET_ACCESS_KEY` | Corresponding S3 secret access key |
| Secret | `CATALOG_SIGNING_KEY_PEM` | Complete Ed25519 private PEM, including BEGIN/END lines |
| Secret | `R2_ENDPOINT_URL` | R2 S3 HTTPS endpoint |
| Secret | `R2_BUCKET` | Image bucket name |

The workflows map `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` to the
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` environment variables used by
the AWS CLI to access R2's S3-compatible API.

The signing private key must match `catalog/keys/catalog-2026-01.pem` and the
public key embedded in the VPS Manager Panel. The publishing workflow checks
this match before signing. Updating the Panel's embedded key requires rebuilding
and deploying the Panel.

1. Run **build OS images** (`build.yml`) with an image key or `all`. The weekly
   schedule builds all registered images, each in its own job.
   It verifies and builds the upstream image, uploads the image, checksum, and provenance,
   attests the image, and then records `build.json`. When the upstream and recipe
   digests already have an attested build, it reuses that build.
2. Copy `object_key` from the run summary. The downloadable Actions artifact
   contains `build.json`, also stored at
   `<os>/<os_version>/<sha256>/build.json`. It records the object and provenance
   paths, upstream and result digests, byte size, format, and the image contract
   from `recipes/linux/contract.json`.
3. Assemble the public HTTPS URL using your download domain and object path.
   The object must be publicly downloadable at that URL.
4. Run **publish image Catalog** (`publish.yml`) with `object_key` and `url`.
   It reads the stored build manifest and downloads the supplied URL on the
   GitHub runner to verify SHA-256 and size. It signs the manifest's image facts
   together with that URL, then uploads `catalog.json` to the `catalog-v1`
   GitHub Release. It updates the selected image while preserving other Catalog
   entries. Catalog publications use the automatic `GITHUB_TOKEN`.

### Retention

After a successful build or Catalog publication, cleanup keeps the newest
successful build for each system/version and the revision referenced by the
current signed Catalog. When they are the same revision, only one is kept.
Successful builds are ordered by the R2 modification time of their `build.json`
completion marker. A successful reuse refreshes that marker, so the selected
revision becomes the newest successful build.

Run **clean up OS images** (`cleanup.yml`) with an image key or `all` to apply
the same policy manually. Cleanup removes obsolete images, checksum files,
provenance, and build manifests. It also removes unfinished uploads under the
revision layout above. Other object paths and files are left untouched.

Cleanup verifies the Catalog signature before deleting anything. HTTP 404 means
no Catalog has been published; other download errors and invalid signatures
stop cleanup. Missing objects in an attested build also stop cleanup.
Before deleting obsolete revisions, cleanup updates `latest.json` from the
newest retained build. If updating a pointer fails, no revisions are deleted.
The manual cleanup workflow can also create or refresh these pointers for
existing successful builds.

Build, publish, and cleanup workflows share the `image-storage` concurrency
group. Each build's image matrix still runs in parallel. Catalog publication and
cleanup cannot overlap uploads or each other.

The runtime Catalog contains the complete URL. A published revision is immutable:
republishing identical facts keeps the current Catalog sequence; changing the
URL or other facts for the same digest fails.

To generate a signed Catalog file from a downloaded build manifest:

```bash
python3 scripts/catalog.py publish --build build.json \
  --url 'https://images.example.com/<object-key>' \
  --public-key catalog/keys/catalog-2026-01.pem --private-key private.pem \
  --output catalog.json
```

When a Catalog already exists, supply it with `--current catalog-current.json`.
The CLI signs metadata; public image byte verification runs in `publish.yml`.

## Release acceptance

Before publishing a new image digest, validate that exact artifact in the real
PVE 9.x and Native environments: import or prewarm, create and reinstall,
initial credentials, disk growth, guest-agent readiness, and image-copy deletion.
Keep the run ID, object key, digest, and results with the release evidence.
These infrastructure checks run separately from `make check` and are not
automated by the build or publishing workflows.

## Public verification

Download `catalog.json`, then verify its canonical manifest with the public key
in `catalog/keys/catalog-2026-01.pem`:

```bash
python3 scripts/catalog.py verify catalog.json \
  --public-key catalog/keys/catalog-2026-01.pem
```

Download the referenced image and compare `sha256sum` and byte size with the
signed revision. GitHub Artifact Attestations bind newly built image bytes to
the public workflow run; `gh attestation verify` verifies that provenance. The
Panel relies on the independent Ed25519 Catalog signature for runtime trust.

## Local checks

`make check` runs ShellCheck, Actionlint, and the Catalog signing checks. Local
commands never resolve, download, customize, convert, boot, or test a real
operating-system image.
