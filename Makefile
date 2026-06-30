# Usage:
#   make setup           # one-time, after cloning
#   make commit m="your message"

.PHONY: setup commit

setup:
	uv sync
	uv run pre-commit install

commit:
	# 1. bump patch version in pyproject.toml (skip re-lock, done below)
	uv version --bump patch --frozen
	# 2. upgrade all dependencies and sync the lock file
	uv lock --upgrade
	# 3. sync virtualenv to the updated lock
	uv sync
	# 4. export production deps to requirements.txt
	uv export --format requirements-txt --no-dev --no-hashes > requirements.txt
	# 5. stage the modified project files, then commit (ruff runs here via pre-commit)
	git add pyproject.toml uv.lock requirements.txt
	git commit -m "$(m)"