"""Run one of N shards of the control tests: python control/tests/shard.py 2 4
(with PYTHONPATH=control). Whole test classes go to a shard, so a class's
fixtures run once; the classes are spread so each shard has about as many
tests. Every shard together runs exactly what unittest discover runs."""
import sys
import unittest
from pathlib import Path


def tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from tests(test)
        else:
            yield test


def main(argv: list[str]) -> int:
    index, count = int(argv[0]), int(argv[1])
    if not 1 <= index <= count:
        raise SystemExit("usage: shard.py INDEX COUNT, with 1 <= INDEX <= COUNT")
    found = unittest.defaultTestLoader.discover(str(Path(__file__).parent))
    classes: dict = {}
    for test in tests(found):
        classes.setdefault(f"{type(test).__module__}.{type(test).__qualname__}", []).append(test)
    shards = [[] for _ in range(count)]
    for name in sorted(classes, key=lambda n: (-len(classes[n]), n)):   # largest first, onto the lightest shard
        min(shards, key=len).extend(classes[name])
    result = unittest.TextTestRunner(verbosity=0).run(unittest.TestSuite(shards[index - 1]))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
