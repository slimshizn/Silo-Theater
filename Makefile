PYTHON ?= python3

.PHONY: build test check

build:
	$(PYTHON) scripts/build.py

test:
	$(PYTHON) -m unittest discover -s tests -v

check:
	$(PYTHON) scripts/build.py --check
	$(PYTHON) -m unittest discover -s tests -v
