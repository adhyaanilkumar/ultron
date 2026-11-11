empty :=
space := $(empty) $(empty)

ifeq ($(OS),Windows_NT)
# The tests spawn `make` and `uv` by name, but MSYS2 make drops its own folder from
# the PATH it hands to child processes. So each recipe's shell puts the make folder
# and the per-user uv folder first, in POSIX form. (make's own $(PATH) is not usable.)
MAKE_BIN := $(shell cygpath -u "$$(cygpath -w "$$(dirname "$$(command -v make)")")")
UV_BIN := $(shell cygpath -u "$$USERPROFILE")/.local/bin
RUN_ENV := env PATH="$(MAKE_BIN):$(UV_BIN):$$PATH"
UV_EXE := $(subst $(space),\$(space),$(UV_BIN))/uv.exe
endif

# uv: the one on PATH, else the per-user install on Windows.
ifneq ($(shell command -v uv 2>/dev/null),)
UV ?= uv
else
UV ?= $(UV_EXE)
endif

.PHONY: build lint test

build:
	$(RUN_ENV) $(UV) sync --locked
	$(RUN_ENV) $(UV) build
	@echo "Build succeeded"

lint:
	$(RUN_ENV) $(UV) run ruff check
	$(RUN_ENV) $(UV) run ruff format --check
	$(RUN_ENV) $(UV) run mypy src
	@echo "Lint succeeded"

test:
	$(RUN_ENV) $(UV) run pytest
	@echo "All tests passed"
