"""Contract tests for the built-in, dependency-free TCP-connect provider."""

from __future__ import annotations

import asyncio
import errno
import socket
import unittest
from time import perf_counter
from unittest import mock

from smart_commissioning_core.engines.ip import (
    ProviderIdentityEvidenceV1,
    TCPProbeRequestV1,
    probe_tcp_connect,
    provider_capability,
)


class _Writer:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class _ConnectedTransport:
    def __init__(self, writer: _Writer) -> None:
        self.writer = writer
        self.calls: list[tuple[str, int, str | None]] = []

    async def connect(
        self,
        target_ip: str,
        port: int,
        *,
        source_ip: str | None,
    ) -> _Writer:
        self.calls.append((target_ip, port, source_ip))
        return self.writer


class _FailingTransport:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def connect(
        self,
        target_ip: str,
        port: int,
        *,
        source_ip: str | None,
    ) -> _Writer:
        raise self.error


class _BlockingTransport:
    async def connect(
        self,
        target_ip: str,
        port: int,
        *,
        source_ip: str | None,
    ) -> _Writer:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _CancellableTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def connect(
        self,
        target_ip: str,
        port: int,
        *,
        source_ip: str | None,
    ) -> _Writer:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


class _BlockingWriter(_Writer):
    async def wait_closed(self) -> None:
        self.waited = True
        await asyncio.Event().wait()


class _FailingCleanupWriter(_Writer):
    async def wait_closed(self) -> None:
        self.waited = True
        raise OSError(errno.EIO, "secret cleanup detail")


class BuiltinTCPProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_connected_result_keeps_protocol_and_closes_transport(self) -> None:
        writer = _Writer()
        transport = _ConnectedTransport(writer)

        result = await probe_tcp_connect(
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=443,
                protocol="tcp",
                source_ip="192.0.2.5",
                application_deadline_s=1.0,
            ),
            transport=transport,
        )

        self.assertEqual(result.provider_contract_version, "1.0")
        self.assertEqual(result.outcome, "connected")
        self.assertEqual(result.protocol, "tcp")
        self.assertEqual(result.port_hint, "https")
        self.assertIsNone(result.detected_service)
        self.assertIsNone(result.detected_version)
        self.assertEqual(transport.calls, [("192.0.2.10", 443, "192.0.2.5")])
        self.assertTrue(writer.closed)
        self.assertTrue(writer.waited)
        self.assertIsNone(result.cleanup_diagnostic)

    async def test_connection_refusal_is_distinct_and_sanitized(self) -> None:
        result = await probe_tcp_connect(
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=443,
                protocol="tcp",
                application_deadline_s=1.0,
            ),
            transport=_FailingTransport(
                ConnectionRefusedError(10061, "secret machine-specific text")
            ),
        )

        self.assertEqual(result.outcome, "connection_refused")
        self.assertEqual(result.diagnostic.code, "connection_refused")
        self.assertEqual(result.diagnostic.errno, 10061)
        self.assertEqual(
            result.diagnostic.reason,
            "The target refused the TCP connection.",
        )
        self.assertNotIn("secret", result.model_dump_json())

    async def test_application_deadline_returns_typed_timeout(self) -> None:
        started = perf_counter()
        result = await probe_tcp_connect(
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=443,
                application_deadline_s=0.02,
            ),
            transport=_BlockingTransport(),
        )

        self.assertEqual(result.outcome, "timed_out")
        self.assertEqual(result.diagnostic.code, "application_deadline_exceeded")
        self.assertIsNone(result.diagnostic.errno)
        self.assertLess(perf_counter() - started, 0.75)

    async def test_one_deadline_also_bounds_writer_cleanup(self) -> None:
        writer = _BlockingWriter()
        started = perf_counter()

        result = await probe_tcp_connect(
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=443,
                application_deadline_s=0.02,
            ),
            transport=_ConnectedTransport(writer),
        )

        self.assertEqual(result.outcome, "connected")
        self.assertEqual(result.diagnostic.code, "connected")
        self.assertEqual(result.cleanup_diagnostic.code, "cleanup_deadline_exceeded")
        self.assertTrue(writer.closed)
        self.assertTrue(writer.waited)
        self.assertLess(perf_counter() - started, 0.75)

    async def test_cleanup_failure_does_not_erase_positive_connection_evidence(self) -> None:
        writer = _FailingCleanupWriter()

        result = await probe_tcp_connect(
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=443,
                application_deadline_s=1.0,
            ),
            transport=_ConnectedTransport(writer),
        )

        self.assertEqual(result.outcome, "connected")
        self.assertEqual(result.diagnostic.code, "connected")
        self.assertEqual(result.cleanup_diagnostic.code, "cleanup_provider_error")
        self.assertEqual(result.cleanup_diagnostic.errno, errno.EIO)
        self.assertNotIn("secret", result.model_dump_json())

    async def test_operating_system_errors_have_stable_typed_outcomes(self) -> None:
        cases = (
            (OSError(errno.ETIMEDOUT, "private timeout text"), "timed_out"),
            (OSError(errno.ENETUNREACH, "private network text"), "network_unreachable"),
            (OSError(errno.EHOSTUNREACH, "private host text"), "host_unreachable"),
            (PermissionError(errno.EACCES, "private permission text"), "permission_denied"),
            (OSError(12345, "private provider text"), "provider_error"),
        )

        for error, expected in cases:
            with self.subTest(expected=expected):
                result = await probe_tcp_connect(
                    TCPProbeRequestV1(
                        target_ip="192.0.2.10",
                        port=443,
                        application_deadline_s=1.0,
                    ),
                    transport=_FailingTransport(error),
                )
                self.assertEqual(result.outcome, expected)
                self.assertEqual(result.diagnostic.code, expected)
                self.assertEqual(result.diagnostic.errno, error.errno)
                self.assertNotIn("private", result.model_dump_json())

    async def test_unexpected_transport_exception_becomes_sanitized_provider_error(self) -> None:
        result = await probe_tcp_connect(
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=443,
                application_deadline_s=1.0,
            ),
            transport=_FailingTransport(RuntimeError("secret adapter detail")),
        )

        self.assertEqual(result.outcome, "provider_error")
        self.assertEqual(result.diagnostic.code, "provider_error")
        self.assertIsNone(result.diagnostic.errno)
        self.assertNotIn("secret", result.model_dump_json())

    async def test_task_cancellation_while_connecting_returns_cancelled_observation(self) -> None:
        transport = _CancellableTransport()
        task = asyncio.create_task(
            probe_tcp_connect(
                TCPProbeRequestV1(
                    target_ip="192.0.2.10",
                    port=443,
                    application_deadline_s=1.0,
                ),
                transport=transport,
            )
        )
        await transport.started.wait()
        task.cancel()

        result = await task

        self.assertEqual(result.outcome, "cancelled")
        self.assertEqual(result.diagnostic.code, "cancelled")
        self.assertEqual(result.diagnostic.reason, "The TCP probe was cancelled.")

    async def test_pre_cancelled_probe_never_reaches_the_transport(self) -> None:
        writer = _Writer()
        transport = _ConnectedTransport(writer)

        result = await probe_tcp_connect(
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=443,
                application_deadline_s=1.0,
            ),
            transport=transport,
            is_cancelled=lambda: True,
        )

        self.assertEqual(result.outcome, "cancelled")
        self.assertEqual(transport.calls, [])
        self.assertFalse(writer.closed)

    async def test_inflight_stop_cancels_the_transport_before_the_socket_deadline(
        self,
    ) -> None:
        transport = _CancellableTransport()
        stop_requested = False

        task = asyncio.create_task(
            probe_tcp_connect(
                TCPProbeRequestV1(
                    target_ip="192.0.2.10",
                    port=443,
                    application_deadline_s=5.0,
                ),
                transport=transport,
                is_cancelled=lambda: stop_requested,
            )
        )
        await transport.started.wait()
        started = perf_counter()
        stop_requested = True

        result = await asyncio.wait_for(task, timeout=1.0)

        self.assertEqual(result.outcome, "cancelled")
        self.assertTrue(transport.cancelled.is_set())
        self.assertLess(perf_counter() - started, 0.75)

    async def test_udp_is_rejected_with_a_typed_capability_result(self) -> None:
        capability = provider_capability(protocol="udp", port=47808)
        self.assertFalse(capability.supported)
        self.assertEqual(capability.requested_protocol, "udp")
        self.assertEqual(capability.reason_code, "unsupported_protocol")
        self.assertEqual(capability.recommended_provider, "bacnet_discovery")

        writer = _Writer()
        transport = _ConnectedTransport(writer)
        result = await probe_tcp_connect(
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=47808,
                protocol="udp",
                application_deadline_s=1.0,
            ),
            transport=transport,
        )

        self.assertEqual(result.outcome, "unsupported")
        self.assertEqual(result.protocol, "udp")
        self.assertEqual(result.capability, capability)
        self.assertEqual(transport.calls, [])
        self.assertFalse(writer.closed)

    async def test_default_transport_uses_numeric_only_ipv4_and_exact_source_bind(self) -> None:
        writer = _Writer()
        with mock.patch(
            "smart_commissioning_core.engines.ip.tcp_connect.asyncio.open_connection",
            new=mock.AsyncMock(return_value=(object(), writer)),
        ) as open_connection:
            result = await probe_tcp_connect(
                TCPProbeRequestV1(
                    target_ip="192.0.2.10",
                    port=443,
                    source_ip="192.0.2.5",
                    application_deadline_s=1.0,
                )
            )

        self.assertEqual(result.outcome, "connected")
        open_connection.assert_awaited_once_with(
            "192.0.2.10",
            443,
            family=socket.AF_INET,
            flags=socket.AI_NUMERICHOST,
            local_addr=("192.0.2.5", 0),
        )

    async def test_hostname_or_non_numeric_source_is_rejected_before_transport(self) -> None:
        with self.assertRaisesRegex(ValueError, "numeric IPv4"):
            TCPProbeRequestV1(
                target_ip="controller.example.test",
                port=443,
                application_deadline_s=1.0,
            )
        with self.assertRaisesRegex(ValueError, "numeric IPv4"):
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=443,
                source_ip="wired-adapter",
                application_deadline_s=1.0,
            )

    async def test_source_bind_failure_has_no_unbound_fallback(self) -> None:
        bind_error = OSError(errno.EADDRNOTAVAIL, "secret adapter name")
        with mock.patch(
            "smart_commissioning_core.engines.ip.tcp_connect.asyncio.open_connection",
            new=mock.AsyncMock(side_effect=bind_error),
        ) as open_connection:
            result = await probe_tcp_connect(
                TCPProbeRequestV1(
                    target_ip="192.0.2.10",
                    port=443,
                    source_ip="192.0.2.5",
                    application_deadline_s=1.0,
                )
            )

        self.assertEqual(result.outcome, "provider_error")
        self.assertEqual(result.diagnostic.errno, errno.EADDRNOTAVAIL)
        self.assertNotIn("secret", result.model_dump_json())
        open_connection.assert_awaited_once_with(
            "192.0.2.10",
            443,
            family=socket.AF_INET,
            flags=socket.AI_NUMERICHOST,
            local_addr=("192.0.2.5", 0),
        )

    async def test_identity_evidence_is_available_to_approved_providers_but_absent_here(self) -> None:
        identity = ProviderIdentityEvidenceV1(
            evidence_kind="protocol_identity",
            confidence="high",
            asset_id="AHU-17",
            hostname="ahu-17.site.test",
            mac_address="02-00-00-00-00-17",
            corroborating_fields=("asset_id", "hostname", "mac_address"),
        )
        self.assertEqual(identity.mac_address, "02:00:00:00:00:17")

        result = await probe_tcp_connect(
            TCPProbeRequestV1(
                target_ip="192.0.2.10",
                port=443,
                application_deadline_s=1.0,
            ),
            transport=_ConnectedTransport(_Writer()),
        )

        self.assertIsNone(result.identity_evidence)


if __name__ == "__main__":
    unittest.main()
