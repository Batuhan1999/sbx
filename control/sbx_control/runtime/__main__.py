"""The installed sandbox runtime, from host scripts.

  python -m sbx_control.runtime guard   its rules on this host's firewall (host-guard.sh)
"""
import argparse

from . import current


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["guard"])
    parser.parse_args(argv)
    current().guard_host()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
