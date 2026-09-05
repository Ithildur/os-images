#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

command -v shellcheck >/dev/null || {
	printf 'shellcheck is required\n' >&2
	exit 1
}
command -v actionlint >/dev/null || {
	printf 'actionlint is required\n' >&2
	exit 1
}

mapfile -t shell_files < <(find "${repo}/scripts" "${repo}/recipes" -type f -name '*.sh' -print | sort)
shellcheck --external-sources --source-path="${repo}" "${shell_files[@]}"
actionlint "${repo}"/.github/workflows/*.yml
python3 -m unittest discover -s "${repo}/tests" -p '*_test.py'
