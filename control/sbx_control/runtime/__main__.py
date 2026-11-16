"""The enabled sandbox runtimes, from host scripts.

  python -m sbx_control.runtime guard     their rules on this host's firewall (host-guard.sh)
  python -m sbx_control.runtime enabled   their kinds, one per line, the default first
"""
import argparse

from . import default, enabled, guard_host


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["guard", "enabled"])
    args = parser.parse_args(argv)
    if args.action == "enabled":
        first = default()
        print("\n".join([first, *(k for k in enabled() if k != first)]))
        return 0
    guard_host()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
