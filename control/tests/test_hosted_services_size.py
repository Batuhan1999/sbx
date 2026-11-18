import unittest
from unittest import mock

from sbx_control import hosted_services


class ContainerSizeTests(unittest.TestCase):
    def size(self, cpus, meminfo):
        with mock.patch.object(hosted_services.os, "cpu_count", return_value=cpus), \
                mock.patch("builtins.open", mock.mock_open(read_data=meminfo)):
            return hosted_services._container_size()

    def test_a_large_host_gets_four_cpus_and_four_gib(self):
        self.assertEqual(self.size(16, "MemTotal:       65000000 kB\n"), (4, 4096))

    def test_a_small_host_is_not_asked_for_more_than_it_has(self):
        # 2 CPUs, ~3.8 GiB: Docker refuses --cpus 4 there.
        self.assertEqual(self.size(2, "MemTotal:        3960000 kB\n"), (2, 1933))

    def test_memory_unknown_keeps_the_default(self):
        with mock.patch.object(hosted_services.os, "cpu_count", return_value=8), \
                mock.patch("builtins.open", side_effect=OSError):
            self.assertEqual(hosted_services._container_size(), (4, 4096))
