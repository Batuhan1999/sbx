"""python -m sbx_control.connectors.amp: the provider's unit and dispatch.sh run this."""
import sys

from .orbs import main

sys.exit(main())
