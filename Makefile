.PHONY: lint fmt lint-install

lint-install:
	uv sync --extra dev

lint:
	uv tool run ruff check .
	uv tool run ruff format --check .
	uv sync --extra dev
	uv run pyright

fmt:
	uv tool run ruff check --fix .
	uv tool run ruff format .
