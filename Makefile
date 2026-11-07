# Use the devenv virtualenv when it exists, otherwise whatever python is on PATH.
PY ?= $(shell if [ -x .devenv/state/venv/bin/python ]; then echo .devenv/state/venv/bin/python; \
              elif command -v python3 >/dev/null; then echo python3; else echo python; fi)
export PYTHONPATH := src

.PHONY: help data doctor sample score train submit validate lint test

help:
	@echo "make data      download docs + masters + 1-month sample (~30 MB)"
	@echo "make doctor    check data layout and config"
	@echo "make sample    build submissions/submission_sample.csv.gz (seconds)"
	@echo "make score     score it with the official scorer (TRAIN has labels)"
	@echo "make train     build + score the full TRAIN phase (minutes, several GB RAM)"
	@echo "make submit PHASE=LIVE FILE=submissions/submission_live.csv.gz"
	@echo "make validate  pre-flight a submission before uploading"

data:
	./scripts/download_data.sh

data-all:
	./scripts/download_data.sh --all

doctor:
	$(PY) -m bankrec doctor

sample:
	$(PY) -m bankrec run --phase sample

score:
	$(PY) -m bankrec score submissions/submission_sample.csv.gz --phase sample

train:
	$(PY) -m bankrec run --phase train --item-phases train
	$(PY) -m bankrec score submissions/submission_train.csv.gz --phase train

live:
	$(PY) -m bankrec run --phase live --item-phases train,live

validate:
	$(PY) -m bankrec validate $(FILE) --phase $(PHASE)

submit:
	$(PY) -m bankrec submit $(PHASE) $(FILE)

lint:
	ruff check src tests

test:
	$(PY) -m pytest -q
