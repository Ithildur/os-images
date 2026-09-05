#!/usr/bin/env bash
set -euo pipefail

die() {
	printf 'build image: %s\n' "$*" >&2
	exit 1
}

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "${GITHUB_ACTIONS:-}" == true && "${VPS_IMAGE_BUILD:-}" == remote-ci ]] ||
	die "real image builds are allowed only in the remote build workflow"
[[ "$#" -eq 4 && "$2" == /* && "$3" == /* && "$4" == /* ]] ||
	die "usage: build-image.sh IMAGE_KEY SOURCE_QCOW2 OUTPUT_QCOW2 METADATA_DIRECTORY"

family="$(python3 "${repo}/scripts/images.py" family "$1")"
source_image="$2"
output_image="$3"
metadata="$4"
[[ -f "${source_image}" && ! -e "${output_image}" ]] || die "source or output path is invalid"
install -d -m 0700 "${metadata}"

export LIBGUESTFS_BACKEND=direct
export LIBGUESTFS_DEBUG=1 LIBGUESTFS_TRACE=1

cp --reflink=auto -- "${source_image}" "${output_image}"
virt-customize -a "${output_image}" \
	--mkdir /var/lib/vps-manager-image \
	--copy-in "${repo}/recipes/linux:/var/lib/vps-manager-image" \
	--run "${repo}/recipes/${family}/customize.sh"
virt-copy-out -a "${output_image}" /var/lib/vps-manager-image/packages.tsv "${metadata}"
virt-customize --no-network -a "${output_image}" \
	--run-command 'rm -rf /var/lib/vps-manager-image'
qemu-img check "${output_image}"
