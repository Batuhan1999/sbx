"""Embed on this host, as install.sh sets it up.

  python -m sbx_control.runtime.embed embed-env [--hugepages N] [--pins FILE]
                                       write the keys sbx owns in Embed's .env
  python -m sbx_control.runtime.embed upgrade-check --source DIR [--hugepages N]
                                       before an upgrade changes anything, and again once
                                       sbx-control is stopped: refuse when installing the
                                       release in DIR recreates Embed under sandboxes
"""
import argparse
import pathlib
import subprocess
import sys

from .host import recreates, write_embed_env


def upgrade_check(source: pathlib.Path, hugepages: int | None = None) -> int:
    """1 when the release in source recreates Embed's running services while
    sandboxes exist, which they do not survive, paused ones included
    (docs/reference.md, Limits); 1 too when that cannot be told."""
    from ... import platform, runtime
    try:
        recreated = recreates(source, hugepages)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"could not compare this release with Embed's running containers: {error}", file=sys.stderr)
        return 1
    if not recreated:
        return 0
    platform.connect()
    from . import Embed
    count = len([i for i in runtime.complete(runtime.sandboxes()) if getattr(i, "runtime", Embed.kind) == Embed.kind])
    if not count:
        print(f"Embed: this release recreates {', '.join(recreated)} (no sandboxes)")
        return 0
    print(f"this release recreates Embed's {', '.join(recreated)}, which the {count} sandbox"
          f"{'es' if count > 1 else ''} here (running or paused) do not survive: delete them first "
          "(sbx down NAME, or the web app), then upgrade again.", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    embed = sub.add_parser("embed-env")
    embed.add_argument("--hugepages", type=int)
    embed.add_argument("--pins", type=pathlib.Path)
    check = sub.add_parser("upgrade-check")
    check.add_argument("--source", type=pathlib.Path, required=True)
    check.add_argument("--hugepages", type=int)
    args = parser.parse_args(argv)
    if args.action == "upgrade-check":
        return upgrade_check(args.source, args.hugepages)
    write_embed_env(hugepages=args.hugepages, pins=args.pins)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
