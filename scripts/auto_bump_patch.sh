#!/usr/bin/env bash
# Auto-increments patch version on every commit, staged into the current commit.
set -euo pipefail

uv run bump-my-version bump patch --no-commit --no-tag --allow-dirty
# Re-lock to record the version bump only — no --upgrade here, that's the
# dedicated uv-lock pre-commit hook's job (see .pre-commit-config.yaml).
uv lock --quiet
git add pyproject.toml uv.lock
