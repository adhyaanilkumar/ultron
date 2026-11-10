UV ?= uv

.PHONY: build lint test

build:
	$(UV) sync --locked
	$(UV) build
	@echo "Build succeeded"

lint:
	$(UV) run ruff check
	$(UV) run ruff format --check
	$(UV) run mypy src
	@echo "Lint succeeded"

test:
	$(UV) run pytest
	@echo "All tests passed"
