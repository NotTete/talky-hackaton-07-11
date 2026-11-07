# Use the devenv virtualenv when it exists, otherwise whatever python is on PATH.
PY ?= $(shell if [ -x .devenv/state/venv/bin/python ]; then echo .devenv/state/venv/bin/python; \
              elif command -v python3 >/dev/null; then echo python3; else echo python; fi)
export PYTHONPATH := src

.PHONY: help data data-lite data-all list verify doctor process evaluate explain push builds validate test lint map map-check whoami result

help:
	@echo "make data        docs + masters + 1-month sample (~10 MB download)"
	@echo "make data-lite   + TRAIN lite (~460 MB)"
	@echo "make data-all    + TRAIN, LIVE, FINAL when published (~1.2 GB)"
	@echo "make list        what is downloaded / extracted / missing"
	@echo ""
	@echo "make process PHASE=sample [NAME=baseline] [TRACE=1]   build an artifact into builds/"
	@echo "make evaluate [BUILD=<id>] [PHASE=sample]   score / coverage-check it"
	@echo "make explain [BUILD=<id>] [EXAMPLES=3]       plain-English report: where lines/pairs are lost"
	@echo "make push PHASE=LIVE [BUILD=<id>]           upload (asks for confirmation)"
	@echo "make whoami                                  check HK_TOKEN, no upload slot used"
	@echo "make result [SUB=s_...]                      read back the scores the platform recorded"
	@echo "make builds                                 list the build repository"
	@echo ""
	@echo "make validate [BUILD=<id>]   structural pre-flight only"
	@echo "make doctor                  data + builds overview"
	@echo "make test                    pytest"

PHASE ?= sample

data:
	$(PY) scripts/download_data.py

data-lite:
	$(PY) scripts/download_data.py --phases train-lite

data-all:
	$(PY) scripts/download_data.py --all

list:
	$(PY) scripts/download_data.py --list

verify:
	$(PY) scripts/download_data.py --verify

doctor:
	$(PY) -m bankrec doctor

process:
	$(PY) -m bankrec process --phase $(PHASE) $(if $(NAME),--name $(NAME),) $(if $(TRACE),--trace,)

evaluate:
	$(PY) -m bankrec evaluate $(BUILD) $(if $(PHASE),--phase $(PHASE),)

explain:
	$(PY) -m bankrec evaluate $(BUILD) $(if $(PHASE),--phase $(PHASE),) --explain --examples $(or $(EXAMPLES),3)

push:
	$(PY) -m bankrec push $(PHASE) $(BUILD)

builds:
	$(PY) -m bankrec builds $(if $(PHASE),--phase $(PHASE),)

validate:
	$(PY) -m bankrec validate $(BUILD) $(if $(PHASE),--phase $(PHASE),)

test:
	$(PY) -m pytest -q

lint:
	ruff check src tests scripts

map:
	$(PY) scripts/build_map.py

map-check:
	$(PY) scripts/build_map.py --check

whoami:
	$(PY) -m bankrec whoami

result:
	$(PY) -m bankrec result $(SUB)
