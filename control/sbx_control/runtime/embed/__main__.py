"""Embed on this host, as install.sh sets it up.

  python -m sbx_control.runtime.embed embed-env [--hugepages N] [--pins FILE]
                                       write the keys sbx owns in Embed's .env
"""
import argparse
import pathlib

from .host import write_embed_env


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    embed = sub.add_parser("embed-env")
    embed.add_argument("--hugepages", type=int)
    embed.add_argument("--pins", type=pathlib.Path)
    args = parser.parse_args(argv)
    write_embed_env(hugepages=args.hugepages, pins=args.pins)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
