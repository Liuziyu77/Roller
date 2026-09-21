# Dual-language Sphinx build. Default HTML output is English; Chinese is under zh/.

SPHINXOPTS    ?=
SPHINXBUILD   ?= sphinx-build
BUILDDIR      = _build

.PHONY: help html clean

help:
	@echo "make html    Build English (site root) and Chinese (/zh/)"
	@echo "make clean   Remove build output"

clean:
	rm -rf "$(BUILDDIR)"

html:
	DOCS_LANG=en $(SPHINXBUILD) -E -a -b html -c . -D language=en $(SPHINXOPTS) en "$(BUILDDIR)/html"
	DOCS_LANG=zh $(SPHINXBUILD) -E -a -b html -c . -D language=zh_CN $(SPHINXOPTS) zh "$(BUILDDIR)/html/zh"
