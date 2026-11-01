import socket
import unittest
from unittest.mock import patch

from software_app.crawlers import common


def dns_result(address):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))]


class ExternalUrlPolicyTests(unittest.TestCase):
    def setUp(self):
        cache = patch.object(common, "_dns_cache", {})
        cache.start()
        self.addCleanup(cache.stop)

    def test_restricted_literals_are_rejected_without_dns_fallback(self):
        for host in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "100.64.0.1", "[::1]"):
            with self.subTest(host=host), patch.object(common, "_resolve_host_ips", return_value=[]) as resolve:
                with self.assertRaisesRegex(ValueError, "受限网络"):
                    common.validate_external_url(f"http://{host}/file")
                resolve.assert_not_called()

    def test_public_literals_do_not_require_dns(self):
        with patch.object(common, "_resolve_host_ips") as resolve:
            for host in ("8.8.8.8", "[2606:4700:4700::1111]"):
                url = f"https://{host}/file"
                self.assertEqual(common.validate_external_url(url), url)
            resolve.assert_not_called()

    def test_empty_invalid_and_mixed_dns_results_are_rejected(self):
        for ips in ([], ["invalid-address"], ["8.8.8.8", "192.168.1.1"]):
            with self.subTest(ips=ips), patch.object(common, "_resolve_host_ips", return_value=ips):
                with self.assertRaises(ValueError):
                    common.validate_external_url("https://files.example/file")

    def test_dns_failure_is_not_cached_and_next_request_can_recover(self):
        with patch.object(
            common.socket, "getaddrinfo", side_effect=[socket.gaierror("failed"), dns_result("8.8.8.8")]
        ) as dns:
            with self.assertRaisesRegex(ValueError, "无法安全解析"):
                common.validate_external_url("https://files.example/file")
            self.assertNotIn("files.example", common._dns_cache)
            self.assertEqual(common.validate_external_url("https://files.example/file"), "https://files.example/file")
            self.assertEqual(dns.call_count, 2)

    def test_expired_resolution_is_checked_again_and_can_become_restricted(self):
        now = [10.0]
        with (
            patch.object(common.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(
                common.socket, "getaddrinfo", side_effect=[dns_result("8.8.8.8"), dns_result("127.0.0.1")]
            ) as dns,
        ):
            url = "https://files.example/file"
            common.validate_external_url(url)
            common.validate_external_url(url)
            self.assertEqual(dns.call_count, 1)
            now[0] += common.DNS_CACHE_TTL + 1
            with self.assertRaisesRegex(ValueError, "受限网络"):
                common.validate_external_url(url)
            self.assertEqual(dns.call_count, 2)

    def test_cache_is_bounded_and_results_cannot_mutate_cached_addresses(self):
        with (
            patch.object(common, "DNS_CACHE_MAX_ENTRIES", 2),
            patch.object(common.socket, "getaddrinfo", return_value=dns_result("8.8.8.8")),
        ):
            for host in ("one.example", "two.example", "three.example"):
                common._resolve_host_ips(host)
            self.assertEqual(len(common._dns_cache), 2)
            self.assertNotIn("one.example", common._dns_cache)
            common._resolve_host_ips("THREE.EXAMPLE.").clear()
            self.assertEqual(common._resolve_host_ips("three.example"), ["8.8.8.8"])
