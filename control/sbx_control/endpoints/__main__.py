"""python -m sbx_control.endpoints nginx FOLDER (tls.sh): the endpoints' stream configuration and CAs there."""
import sys
from pathlib import Path

from .nginx import write_nginx

if sys.argv[1:2] == ["nginx"] and len(sys.argv) == 3:
    write_nginx(Path(sys.argv[2]))
else:
    raise SystemExit("usage: python -m sbx_control.endpoints nginx FOLDER")
