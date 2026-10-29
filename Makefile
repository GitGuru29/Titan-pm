PYTHON ?= python3
SRC_ROOT ?= ../ArchTitan-OS/airootfs
SIGN_KEY ?=
ARCH ?= x86_64

.PHONY: help install dev test lint typecheck fmt build repo clean

help:
	@echo "titanpkg targets:"
	@echo "  make dev       install dev dependencies into .venv"
	@echo "  make test      run unit + integration tests"
	@echo "  make lint      run ruff"
	@echo "  make typecheck run mypy"
	@echo "  make fmt       format with ruff"
	@echo "  make build     build .tpkg packages from packaging/manifests"
	@echo "  make repo      build and index the repository into dist/repo"
	@echo "  make clean     remove build artefacts"

dev:
	$(PYTHON) -m venv .venv
	./.venv/bin/pip install -U pip
	./.venv/bin/pip install -e '.[dev]'

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests packaging

typecheck:
	$(PYTHON) -m mypy

fmt:
	$(PYTHON) -m ruff format src tests packaging

build:
	./packaging/build-packages.sh --source-root "$(SRC_ROOT)" --arch "$(ARCH)" --no-repo

repo:
	./packaging/build-packages.sh --source-root "$(SRC_ROOT)" --arch "$(ARCH)" \
		--repo dist/repo $(if $(SIGN_KEY),--sign-key "$(SIGN_KEY)",)

clean:
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
