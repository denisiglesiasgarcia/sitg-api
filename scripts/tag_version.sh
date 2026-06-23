#!/usr/bin/env bash
# Tags the commit with the current version after it is created.
set -euo pipefail

VERSION=$(uv run bump-my-version show current_version)
git tag "v${VERSION}"
echo "Tagged: v${VERSION}"
