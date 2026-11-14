"""GC roots follow the builds templates and snapshots point at now, not a
snapshot's legacy source or superseded tag history; what nothing keeps goes
at once unless Embed is writing a build."""
import importlib.util
import pathlib
import tempfile
import unittest
from unittest.mock import patch

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


class DoomedTests(unittest.TestCase):
    NOW = 1_000_000.0

    def test_unkept_layers_go_at_once_while_nothing_is_written(self):
        # A failed build's layers (22-29 GB for n8n) waited two hours before.
        builds, keep = {'kept', 'failed-step', 'old'}, {'kept'}
        age = {'kept': self.NOW, 'failed-step': self.NOW - 60, 'old': self.NOW - 3 * 3600}.get
        self.assertEqual(gc.doomed_builds(builds, keep, age, busy=False, now=self.NOW), ['failed-step', 'old'])

    def test_while_a_build_is_written_young_layers_stay(self):
        builds, keep = {'kept', 'running-step', 'old'}, {'kept'}
        age = {'kept': self.NOW, 'running-step': self.NOW - 60, 'old': self.NOW - 3 * 3600}.get
        self.assertEqual(gc.doomed_builds(builds, keep, age, busy=True, now=self.NOW), ['old'])

    def test_nothing_is_deleted_while_compaction_holds_the_storage_lock(self):
        # flock conflicts between two opens of the file, in one process as in two.
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(gc.storage, 'LOCK', pathlib.Path(directory) / 'lock'), \
                    patch.object(gc, 'collect') as collect:
                with gc.storage.lock() as held:
                    self.assertTrue(held)
                    gc.main()
                    collect.assert_not_called()
                gc.main()
                collect.assert_called_once()

if __name__ == '__main__':
    unittest.main()
