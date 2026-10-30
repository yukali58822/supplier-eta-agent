.PHONY: install test eval generate demo check up down

PY ?= uv run

install:
	uv sync --extra dev

test:
	$(PY) pytest

generate:
	$(PY) python -m eta_agent.cli generate --out data/emails

eval:
	$(PY) python -m eta_agent.cli eval --data data/emails --out reports

demo:
	$(PY) python -m eta_agent.cli demo

check:
	$(PY) python -m eta_agent.cli check-llm

up:
	docker compose up --build

down:
	docker compose down
