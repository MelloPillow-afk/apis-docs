.PHONY: install docs check test

install:
	uv sync --locked

# Regenerate site/ from specs/.
docs:
	uv run --locked python scripts/build_api_docs.py

# Fail when site/ no longer matches specs/.
check: test
	uv run --locked python scripts/build_api_docs.py --check

test:
	uv run --locked pytest
