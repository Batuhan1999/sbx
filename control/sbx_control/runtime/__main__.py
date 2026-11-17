"""The enabled sandbox runtimes, from host scripts.

  python -m sbx_control.runtime guard     their rules on this host's firewall (host-guard.sh)
  python -m sbx_control.runtime enabled   their kinds, one per line, the default first
  python -m sbx_control.runtime env [--enable KIND]... NAME...
                                          install.sh: set each NAME of /etc/sbx/runtime.env to its
                                          value in this process's environment (unset or empty:
                                          remove the line; values never on the command line), and
                                          enable each KIND (SBX_RUNTIMES); every other line stays
"""
import argparse
import os

from . import ENABLED_KEY, FALLBACK, configuration, default, enabled, guard_host


def write_env(names: list, kinds: list, environ=os.environ) -> list:
    """runtime.env with names set from environ and kinds enabled, in
    order after those it enables already (a file without SBX_RUNTIMES
    enables Embed alone, as ever: that stays so). The kinds now enabled."""
    from .. import paths, vault
    file = paths.runtime_env()
    existed = file.exists()
    listed = [k.strip() for k in configuration()[ENABLED_KEY].replace(" ", ",").split(",") if k.strip()]
    now = list(dict.fromkeys(listed or ([FALLBACK] if existed else [])))
    wanted = list(dict.fromkeys([*now, *kinds]))
    values = {name: environ.get(name) or None for name in names}
    if wanted != now or (not existed and wanted):
        values[ENABLED_KEY] = ",".join(wanted)
    if not existed:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch(mode=0o600)
        file.write_text("# This host's sandbox runtimes and their keys. Written by install.sh and sbx-admin "
                        "runtime; not backed up.\n")
    vault.edit_runtime_file(values)
    return wanted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("guard")
    sub.add_parser("enabled")
    env = sub.add_parser("env")
    env.add_argument("--enable", action="append", default=[], metavar="KIND")
    env.add_argument("names", nargs="*")
    args = parser.parse_args(argv)
    if args.action == "enabled":
        first = default()
        print("\n".join([first, *(k for k in enabled() if k != first)]))
        return 0
    if args.action == "env":
        print(f"runtimes: {', '.join(write_env(args.names, args.enable))}")
        return 0
    guard_host()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
