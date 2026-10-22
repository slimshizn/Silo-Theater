PYTHON ?= python3

.PHONY: build repository test check

build:
	$(PYTHON) scripts/build.py

repository:
	$(PYTHON) scripts/build_repository.py

test:
	$(PYTHON) -m unittest discover -s tests -v

check:
	$(PYTHON) scripts/build.py --check
	$(PYTHON) -m unittest discover -s tests -v
