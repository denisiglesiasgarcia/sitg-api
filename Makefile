# Usage:
#   make setup           # one-time, after cloning
#   make commit m="your message"

.PHONY: setup commit

setup:
	uv sync
	uv run pre-commit install

commit:
	# Version bump, lock upgrade, sync, and requirements.txt export all happen
	# automatically via pre-commit hooks (see .pre-commit-config.yaml) —
	# doing them here too would bump the patch version twice.
	git commit -m "$(m)"
