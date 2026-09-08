#!/bin/sh
# The runner image's entrypoint: hand straight off to the Python module
# that owns the copy/pytest/marker/hold protocol (hokea.runner_entry —
# kept in Python so the protocol is unit-testable outside the image).
# -u: unbuffered, so `kubectl logs -f` streams pytest output live.
exec python -u -m hokea.runner_entry "$@"
