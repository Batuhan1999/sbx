"""python -m sbx_control.settings shell | secret NAME | get [SECTION]
For the host's shell scripts (tls.sh, host-install.sh, host.py,
sbx-host-guard, a connector's own) and for a look. Changes go through
sbx-admin. A secret NAME is a platform secret, or <extension>:<NAME> for an
extension's own."""
import json
import sys

from .. import extensions, vault
from . import current, shell_lines


def main(argv: list) -> int:
    command = argv[:1]
    if command == ["shell"]:
        s = current()
        from .. import endpoints
        print("\n".join(shell_lines(s, {c.kind: c.settings() for c in extensions.connectors() if c.section},
                                     endpoints.public_ports())))
    elif command == ["secret"] and len(argv) == 2:
        value = vault.get(vault.key(argv[1])) or ""
        if not value:
            print(f"no {argv[1]} in the vault", file=sys.stderr)
            return 1
        print(value)
    elif command == ["get"]:
        doc = current().model_dump(mode="json")
        print(json.dumps(doc.get(argv[1], doc["extensions"].get(argv[1])) if argv[1:] else doc, indent=2))
    else:
        print("usage: python -m sbx_control.settings shell | secret NAME | get [SECTION]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
