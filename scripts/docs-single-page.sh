#!/usr/bin/env bash
# Build the whole documentation site as ONE self-contained HTML file:
#   dist/hokea-docs.html
# No external fetches (fonts, CDNs, XHR) and no references to other local
# files — suitable for offline use or environments that block all network
# requests. Inline CSS and inline JavaScript are fine; the file carries a
# small hand-written navigation layer (floating table of contents).
#
# Mechanism, in three steps:
#   1. Build the real multi-page site (site/) with --strict, exactly as
#      always — this is the config students' `mkdocs serve` uses, and the
#      export must never leave it broken.
#   2. Build again with mkdocs-print.yml (site-export/): same pages, but
#      nav in the single-page READING order (introductory lab ->
#      guides -> API reference), since mkdocs-print-site
#      concatenates pages in nav order.
#   3. scripts/docs_single_page.py turns site-export/print_page/index.html
#      into the final file: inlines all CSS, strips the multi-page-only
#      Material JS and chrome, repairs anchors, injects the navigation
#      layer, and runs the self-containment check (which also verifies
#      every in-page link resolves).
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --group docs mkdocs build --strict
uv run --group docs mkdocs build --strict -f mkdocs-print.yml
uv run --group docs python scripts/docs_single_page.py
