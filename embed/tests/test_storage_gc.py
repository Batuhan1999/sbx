"""GC roots follow the builds templates and snapshots point at now, not a
snapshot's legacy source or superseded tag history."""
import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location('storage_gc', pathlib.Path(__file__).parents[1] / 'storage-gc.py')
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


def roots(assignments=(), snapshots=(), finished=None):
    return {tuple(row) for row in gc.reference_roots([list(a) for a in assignments], list(snapshots), finished or {})}


class ReferenceRootTests(unittest.TestCase):
    def test_snapshot_builds_are_roots_whatever_their_source(self):
        finished = {'A': 'f', 'B': 'f', 'newer': 'f'}
        self.assertEqual(roots(snapshots=['A', 'B'], finished=finished), {('A', 'f'), ('B', 'f')})
        self.assertEqual(roots(snapshots=['B'], finished=finished), {('B', 'f')})

    def test_only_each_tags_newest_assignment_counts(self):
        finished = {'old': 'f', 'current': 'f', 'pinned': 'f'}
        newest_first = [('template', 'current', 'latest'), ('template', 'old', 'latest'), ('template', 'pinned', 'stable')]
        self.assertEqual(roots(assignments=newest_first, finished=finished), {('current', 'f'), ('pinned', 'f')})

    def test_an_unfinished_build_does_not_replace_the_finished_one_and_recent_ones_are_marked(self):
        newest_first = [('template', 'failed', 'latest'), ('template', 'success', 'latest')]
        self.assertEqual(roots(assignments=newest_first, snapshots=['new', 'building'], finished={'success': 'f', 'new': 't'}),
                         {('success', 'f'), ('new', 't')})


if __name__ == '__main__':
    unittest.main()
