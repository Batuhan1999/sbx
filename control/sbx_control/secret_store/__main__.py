"""python -m sbx_control.secret_store migrate
Move what the local store holds into the chosen store (Infisical, once
`infisical setup` pointed the settings at it): every namespace's values,
every sealed column, and the runtime's API key. Values are verified in the
new store before their local copy goes; nothing is printed of them."""
import sys

from .. import state, vault
from . import migrate, store


def main(argv) -> int:
    if argv != ["migrate"]:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    state.migrate()
    if store().name == "local":
        print("the store is local: nothing to move (sbx-admin store enable)", file=sys.stderr)
        return 1
    moved = migrate() + vault.seal_runtime_file()
    print(f"secret store: verified and moved {moved} values to {store().name} (values withheld)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
