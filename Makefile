.PHONY: install test run receiver clean

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

install:  ## Create venv and install dependencies
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

test:  ## Run the full test suite (fast, deterministic, no network)
	$(PY) -m pytest -q -W ignore::DeprecationWarning

run:  ## Run the webhook service + background worker on :8000
	$(PY) -m app.main

receiver:  ## Run the configurable test receiver on :9000
	$(PY) -m receiver.app

clean:  ## Remove local SQLite state and caches
	rm -f *.db *.db-wal *.db-shm
	rm -rf .pytest_cache **/__pycache__
