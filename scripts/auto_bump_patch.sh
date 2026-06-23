#!/usr/bin/env bash
# Auto-increments patch version on every commit, staged into the current commit.
set -euo pipefail

uv run bump-my-version bump patch --no-commit --no-tag --allow-dirty
uv lock --upgrade --quiet
git add pyproject.toml uv.lock
