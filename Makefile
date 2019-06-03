#!/usr/bin/make -f

.PHONY: doc test update all tag pypi upload

all:
		@echo "Please use 'python setup.py'."
		@exit 1

# need to use python3 sphinx-build
PATH := /usr/share/sphinx/scripts/python3:${PATH}

PACKAGE = asyncari
PYTHON ?= python3
export PYTHONPATH=$(shell pwd)

PYTEST ?= ${PYTHON} $(shell which pytest-3)
TEST_OPTIONS ?= -xvvv --full-trace
PYLINT_RC ?= .pylintrc

BUILD_DIR ?= build
INPUT_DIR ?= docs/source

# Sphinx options (are passed to build_docs, which passes them to sphinx-build)
#   -W       : turn warning into errors
#   -a       : write all files
#   -b html  : use html builder
#   -i [pat] : ignore pattern

SPHINXOPTS ?= -a -W -b html
AUTOSPHINXOPTS := -i *~ -i *.sw* -i Makefile*

SPHINXBUILDDIR ?= $(BUILD_DIR)/sphinx/html
ALLSPHINXOPTS ?= -d $(BUILD_DIR)/sphinx/doctrees $(SPHINXOPTS) docs

doc:
	sphinx3-build -a $(INPUT_DIR) $(BUILD_DIR)

livehtml: docs
	sphinx-autobuild $(AUTOSPHINXOPTS) $(ALLSPHINXOPTS) $(SPHINXBUILDDIR)

test:
	$(PYTEST) $(PACKAGE) $(TEST_OPTIONS)


tagged:
	git describe --tags --exact-match
	test $$(git ls-files -m | wc -l) = 0

pypi:   tagged
	python3 setup.py sdist upload

upload: pypi
	git push-all --tags

update:
	pip install -r ci/test-requirements.txt

