"""Source-interface identity selection and execution-preflight tests.

The tests use frozen in-memory interface inventories.  No real adapter is
inspected and no packet is sent; the only socket operation permitted on the
green path is a bind-only availability proof.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

from smart_commissioning_core.source_interface import (
    SOURCE_INTERFACE_IDENTITY_KEY,
    FrozenSourceInterfaceV1,
    SourceInterfaceCandidateV1,
    enumerate_source_interfaces,
    guard_frozen_source_interface,
    select_source_interface,
    source_interface_resource_key,
)


def _candidate(
    *,
    interface_id: str,
    name: str,
    source_ip: str,
    prefix_length: int = 24,
    metric: int | None,
    is_up: bool = True,
) -> SourceInterfaceCandidateV1:
    return SourceInterfaceCandidateV1(
        interface_id=interface_id,
        interface_name=name,
        source_ip=source_ip,
        prefix_length=prefix_length,
        is_up=is_up,
        default_route_metric=metric,
    )


class SourceInterfaceResourceKeyTests(unittest.TestCase):
    def test_key_binds_executor_scope_and_stable_interface_only(self) -> None:
        base = FrozenSourceInterfaceV1(
            selection="explicit",
            executor_scope="field-executor-a",
            interface_id="if-guid-7",
            interface_name="Field NIC",
            source_ip="192.0.2.10",
            prefix_length=24,
            local_address="192.0.2.10/24",
        )
        same_interface_other_address = FrozenSourceInterfaceV1(
            selection="default_route",
            executor_scope="field-executor-a",
            interface_id="if-guid-7",
            interface_name="Renamed Field NIC",
            source_ip="192.0.2.11",
            prefix_length=24,
            local_address="192.0.2.11/24",
            default_route_metric=20,
        )
        different_scope = base.model_copy(update={"executor_scope": "field-executor-b"})
        different_interface = base.model_copy(update={"interface_id": "if-guid-8"})

        key = source_interface_resource_key(base)
        self.assertRegex(key, r"^nic:v1:[0-9a-f]{64}$")
        self.assertEqual(key, source_interface_resource_key(same_interface_other_address))
        self.assertNotEqual(key, source_interface_resource_key(different_scope))
        self.assertNotEqual(key, source_interface_resource_key(different_interface))


@unittest.skipUnless(sys.platform == "win32", "Windows IP Helper API contract")
class WindowsSourceInterfaceResolutionTests(unittest.TestCase):
    def test_inventory_starts_no_process_and_opens_no_network_socket(self) -> None:
        forbidden = AssertionError("source-interface resolution crossed a traffic boundary")
        with (
            mock.patch.object(subprocess, "run", side_effect=forbidden),
            mock.patch.object(subprocess, "Popen", side_effect=forbidden),
            mock.patch("socket.getaddrinfo", side_effect=forbidden),
            mock.patch("socket.create_connection", side_effect=forbidden),
            mock.patch("socket.socket", side_effect=forbidden),
        ):
            candidates = enumerate_source_interfaces()

        self.assertIsInstance(candidates, list)


class SelectSourceInterfaceTests(unittest.TestCase):
    def test_default_route_uses_lowest_metric_then_stable_identity_order(self) -> None:
        selected = select_source_interface(
            [
                _candidate(
                    interface_id="windows-guid:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    name="Wi-Fi",
                    source_ip="192.0.2.20",
                    metric=15,
                ),
                _candidate(
                    interface_id="windows-guid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    name="Ethernet B",
                    source_ip="192.0.2.11",
                    metric=5,
                ),
                _candidate(
                    interface_id="windows-guid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    name="Ethernet A",
                    source_ip="192.0.2.10",
                    metric=5,
                ),
                _candidate(
                    interface_id="windows-guid:cccccccc-cccc-cccc-cccc-cccccccccccc",
                    name="Down route",
                    source_ip="192.0.2.30",
                    metric=1,
                    is_up=False,
                ),
            ],
            selection="default_route",
            executor_scope="edge-london-1",
        )

        self.assertEqual(
            selected.model_dump(mode="json"),
            {
                "schema_version": "1.0",
                "selection": "default_route",
                "executor_scope": "edge-london-1",
                "interface_id": "windows-guid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "interface_name": "Ethernet A",
                "source_ip": "192.0.2.10",
                "prefix_length": 24,
                "local_address": "192.0.2.10/24",
                "default_route_metric": 5,
            },
        )

    def test_explicit_selection_requires_the_requested_address_and_prefix(self) -> None:
        selected = select_source_interface(
            [
                _candidate(
                    interface_id="linux-ifindex:2",
                    name="eth0",
                    source_ip="10.20.30.7",
                    prefix_length=24,
                    metric=None,
                )
            ],
            selection="explicit",
            source_ip="10.20.30.7",
            prefix_length=24,
            executor_scope="portable-a",
        )
        self.assertEqual(selected.interface_id, "linux-ifindex:2")
        self.assertEqual(selected.local_address, "10.20.30.7/24")

        with self.assertRaisesRegex(ValueError, "prefix /16"):
            select_source_interface(
                [
                    _candidate(
                        interface_id="linux-ifindex:2",
                        name="eth0",
                        source_ip="10.20.30.7",
                        prefix_length=24,
                        metric=None,
                    )
                ],
                selection="explicit",
                source_ip="10.20.30.7",
                prefix_length=16,
                executor_scope="portable-a",
            )


class GuardFrozenSourceInterfaceTests(unittest.TestCase):
    def _parameters(self, frozen: FrozenSourceInterfaceV1) -> dict[str, object]:
        return {
            "source_ip": frozen.source_ip,
            "local_address": frozen.local_address,
            SOURCE_INTERFACE_IDENTITY_KEY: frozen.model_dump(mode="json"),
        }

    def _frozen_default(self) -> FrozenSourceInterfaceV1:
        return select_source_interface(
            [
                _candidate(
                    interface_id="windows-guid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    name="Ethernet",
                    source_ip="192.168.10.20",
                    metric=10,
                )
            ],
            selection="default_route",
            executor_scope="edge-a",
        )

    def test_default_route_change_fails_before_bind(self) -> None:
        current = [
            _candidate(
                interface_id="windows-guid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                name="Ethernet",
                source_ip="192.168.10.20",
                metric=50,
            ),
            _candidate(
                interface_id="windows-guid:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                name="Wi-Fi",
                source_ip="10.0.0.20",
                metric=5,
            ),
        ]
        with (
            mock.patch(
                "smart_commissioning_core.source_interface.enumerate_source_interfaces",
                return_value=current,
            ),
            mock.patch("smart_commissioning_core.source_interface.socket.socket") as socket_factory,
        ):
            with self.assertRaisesRegex(ValueError, "default route changed"):
                guard_frozen_source_interface(
                    self._parameters(self._frozen_default()),
                    expected_executor_scope="edge-a",
                )
        socket_factory.assert_not_called()

    def test_nic_disappearance_fails_before_bind(self) -> None:
        with (
            mock.patch(
                "smart_commissioning_core.source_interface.enumerate_source_interfaces",
                return_value=[],
            ),
            mock.patch("smart_commissioning_core.source_interface.socket.socket") as socket_factory,
        ):
            with self.assertRaisesRegex(ValueError, "no longer present"):
                guard_frozen_source_interface(
                    self._parameters(self._frozen_default()),
                    expected_executor_scope="edge-a",
                )
        socket_factory.assert_not_called()

    def test_name_address_or_prefix_drift_fails_before_bind(self) -> None:
        frozen = self._frozen_default()
        for candidate in (
            _candidate(
                interface_id=frozen.interface_id,
                name="Ethernet renamed",
                source_ip=frozen.source_ip,
                prefix_length=frozen.prefix_length,
                metric=10,
            ),
            _candidate(
                interface_id=frozen.interface_id,
                name=frozen.interface_name,
                source_ip="192.168.10.21",
                prefix_length=frozen.prefix_length,
                metric=10,
            ),
            _candidate(
                interface_id=frozen.interface_id,
                name=frozen.interface_name,
                source_ip=frozen.source_ip,
                prefix_length=16,
                metric=10,
            ),
        ):
            with self.subTest(candidate=candidate), mock.patch(
                "smart_commissioning_core.source_interface.enumerate_source_interfaces",
                return_value=[candidate],
            ), mock.patch(
                "smart_commissioning_core.source_interface.socket.socket"
            ) as socket_factory:
                with self.assertRaisesRegex(ValueError, "identity drifted"):
                    guard_frozen_source_interface(
                        self._parameters(frozen),
                        expected_executor_scope="edge-a",
                    )
                socket_factory.assert_not_called()

    def test_metric_only_default_route_change_fails_before_bind(self) -> None:
        frozen = self._frozen_default()
        current = [
            _candidate(
                interface_id=frozen.interface_id,
                name=frozen.interface_name,
                source_ip=frozen.source_ip,
                prefix_length=frozen.prefix_length,
                metric=99,
            )
        ]
        with (
            mock.patch(
                "smart_commissioning_core.source_interface.enumerate_source_interfaces",
                return_value=current,
            ),
            mock.patch("smart_commissioning_core.source_interface.socket.socket") as socket_factory,
        ):
            with self.assertRaisesRegex(ValueError, "default route changed"):
                guard_frozen_source_interface(
                    self._parameters(frozen),
                    expected_executor_scope="edge-a",
                )
        socket_factory.assert_not_called()

    def test_executor_scope_mismatch_fails_before_inventory_or_bind(self) -> None:
        with (
            mock.patch(
                "smart_commissioning_core.source_interface.enumerate_source_interfaces"
            ) as enumerate_interfaces,
            mock.patch("smart_commissioning_core.source_interface.socket.socket") as socket_factory,
        ):
            with self.assertRaisesRegex(ValueError, "executor identity"):
                guard_frozen_source_interface(
                    self._parameters(self._frozen_default()),
                    expected_executor_scope="edge-b",
                )
        enumerate_interfaces.assert_not_called()
        socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
