"""Previews of an installation with an IP and no domain: a stable
https://<ip>:<port> per sandbox and port, allocated, reclaimed and gated."""
import concurrent.futures
from pathlib import Path
from unittest.mock import patch

from support import Installation


class IpPreviewTests(Installation):
    SETTINGS = {"domain": {"address": "203.0.113.10"}}

    def setUp(self):
        super().setUp()
        from sbx_control import ip_previews
        self.ip = ip_previews

    def sandbox(self, name, vm):
        """A registered sandbox whose workspace is ready on the VM."""
        from sbx_control import lifecycle, sandboxes
        sandboxes.register(name, "owner@example.com", "main", name)
        op = lifecycle.begin(name, "create")
        op.advance("allocating", sandbox_id=vm)
        op.advance("preparing_workspace")
        op.advance("workspace_ready", finish=True)

    def at(self, seconds):
        return patch.object(self.ip.time, "time", return_value=seconds)

    def test_concurrent_allocations_are_stable_and_independent(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            urls = list(executor.map(lambda i: self.ip.origin('first' if i % 2 else 'second', 3000), range(32)))
        self.assertEqual(len(set(urls)), 2)
        first = self.ip.origin('first', 3000)
        self.assertNotEqual(first, self.ip.origin('first', 4000))
        self.assertEqual(first, self.ip.origin('first', 3000))

    def test_deletion_revokes_only_owned_routes(self):
        first = self.ip.origin('first', 3000)
        second = self.ip.origin('second', 3000)
        self.ip.remove('first')
        self.assertFalse(self.ip.allowed_url(first))
        self.assertIsNone(self.ip.route(int(first.rsplit(':', 1)[1])))
        self.assertEqual(self.ip.origin('first', 3000), '')
        self.ip.activate('first')
        self.assertTrue(self.ip.origin('first', 3000))
        self.assertTrue(self.ip.allowed_url(second))

    def test_login_returns_only_to_an_allocated_https_preview(self):
        url = self.ip.origin('first', 3000)
        self.assertTrue(self.ip.allowed_url(url + '/login?next=%2Fprojects'))
        for value in (url.replace('https:', 'http:'), url.replace('203.0.113.10', 'evil.example'),
                      url.replace('https://', 'https://user@'), 'https://203.0.113.10:1/',
                      url + '/\\evil.example', url + '/\r\nLocation: https://evil.example'):
            with self.subTest(value=value):
                self.assertFalse(self.ip.allowed_url(value))
        port = int(url.rsplit(':', 1)[1])
        self.assertIn('%3Fone%3D1%26two%3D2', self.ip.login_url(port, '/?one=1&two=2'))

    def test_ssh_bridge_gets_an_assigned_port_without_a_domain(self):
        from sbx_control import addressing, recipes, settings
        a = recipes.App('owner/app', Path('/unused'), {'previews': {'': 3000, 'ssh': {'port': 8081, 'link': False}}})
        url = addressing.ssh_url('first', a, 'sandbox-id')
        self.assertEqual(url, self.ip.origin('first', 8081).replace('https://', 'wss://'))
        self.assertTrue(self.ip.route(int(url.rsplit(':', 1)[1])))
        settings.update('domain', {'address': '203.0.113.10', 'name': 'sbx.example'}, by='test')
        self.assertEqual(addressing.ssh_url('first', a, 'sandbox-id'), 'wss://8081-sandbox-id.sbx.example')

    def test_control_cookie_never_reaches_an_application(self):
        self.assertEqual(self.ip.app_cookies('app=a; sbx_session=first; other=b; sbx_session=second'),
                         'app=a; other=b')

    def test_the_preview_server_gates_every_request_and_strips_what_the_app_must_not_see(self):
        config_text = self.ip.nginx('203.0.113.10', '203.0.113.10')
        self.assertIn('auth_request /_sbx_ip_gate;', config_text)
        self.assertIn('proxy_set_header Cookie $ip_cookies;', config_text)     # app_cookies, without the control's
        self.assertIn('proxy_set_header E2b-Sandbox-Id "";', config_text)      # the gate, not the client, picks the VM

    def test_ipv6(self):
        self.assertEqual(self.ip.host('2001:db8::1'), '[2001:db8::1]')
        self.assertIn('listen [::]:', self.ip.nginx('2001:db8::1', '2001:db8::1'))

    def test_exhaustion_is_actionable(self):
        from sbx_control import settings
        settings.update('network', {'ip_preview_first_port': 22000, 'ip_preview_last_port': 22000}, by='test')
        self.ip.origin('first', 3000)
        with self.assertRaisesRegex(ValueError, 'delete unused sandboxes or connect a domain'):
            self.ip.origin('second', 3000)

    def test_reclaims_only_continuously_missing_detected_ports(self):
        self.sandbox('a', 'a-vm')
        declared = self.ip.origin('a', 3000)
        active = self.ip.origin('a', 4000)
        transient = self.ip.origin('a', 5000)
        stale = self.ip.origin('a', 6000)
        with self.at(1000):
            self.assertEqual(self.ip.observe('a-vm', [3000], {4000: False}), 0)
        with self.at(1100):
            self.assertEqual(self.ip.observe('a-vm', [3000], {4000: False, 5000: False}), 0)
        with self.at(1301):
            self.assertEqual(self.ip.observe('a-vm', [3000], {4000: False, 5000: False}), 1)
        self.assertFalse(self.ip.allowed_url(stale))
        self.assertEqual(self.ip.origin('a', 3000), declared)
        self.assertEqual(self.ip.origin('a', 4000), active)
        self.assertEqual(self.ip.origin('a', 5000), transient)

    def test_obsolete_allocation_cannot_reclaim_current_routes(self):
        url = self.ip.origin('a', 4000)
        self.sandbox('a', 'new-vm')
        with self.at(1000):
            self.ip.observe('new-vm', [], {})
        with self.at(10000):
            self.assertEqual(self.ip.observe('old-vm', [], {}), 0)
        self.assertTrue(self.ip.allowed_url(url))

    def test_configured_range_drives_allocation_tls_and_firewall(self):
        from sbx_control import settings
        settings.update('network', {'ip_preview_first_port': 22000, 'ip_preview_last_port': 22003}, by='test')
        self.assertTrue(self.ip.origin('a', 3000).endswith(':22000'))
        config_text = self.ip.nginx('203.0.113.10', '203.0.113.10')
        self.assertIn('listen 22003 ssl;', config_text)
        self.assertNotIn('listen 22004 ssl;', config_text)
        self.assertIn('22000:22003', '\n'.join(settings.shell_lines(settings.current())))   # host-guard opens them
        with self.assertRaisesRegex(settings.Invalid, 'exclude assigned URLs'):
            settings.update('network', {'ip_preview_first_port': 22001, 'ip_preview_last_port': 22003}, by='test')
        settings.update('network', {'ip_preview_first_port': 22000, 'ip_preview_last_port': 22001}, by='test')
        self.assertTrue(self.ip.allowed_url(self.ip.origin('a', 3000)))
        for value in ({'ip_preview_first_port': 22000, 'ip_preview_last_port': 21000},
                      {'ip_preview_first_port': 80},
                      {'public_ports': [21001]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.Network(**value)
