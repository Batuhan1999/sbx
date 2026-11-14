#!/bin/sh
set -eu
# The Amp provider runs this for every job.
cd "$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
exec venv/bin/python -m sbx_control.connectors.amp dispatch
