"""Parameter binding tests for concrete source-interface identity."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.engine_dispatch import resolve_source_interface
from smart_commissioning_core.source_interface import (
    SOURCE_INTERFACE_IDENTITY_KEY,
    FrozenSourceInterfaceV1,
)


def _identity(
    *,
    selection: str,
    source_ip: str,
    prefix_length: int,
) -> FrozenSourceInterfaceV1:
    return FrozenSourceInterfaceV1(
        selection=selection,
        executor_scope="edge-london-1",
        interface_id="windows-guid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        interface_name="Ethernet 3",
        source_ip=source_ip,
        prefix_length=prefix_length,
        local_address=f"{source_ip}/{prefix_length}",
        default_route_metric=15 if selection == "default_route" else None,
    )


class ResolveSourceInterfaceTests(unittest.TestCase):
    def test_auto_and_omitted_values_freeze_the_concrete_default_route(self) -> None:
        frozen = _identity(
            selection="default_route",
            source_ip="192.168.10.20",
            prefix_length=24,
        )
        for configured in (None, "", "   ", "Auto (OS default route)", "auto (os default route)"):
            with self.subTest(configured=configured), patch(
                "app.services.engine_dispatch.interface_service.resolve_source_interface_identity",
                return_value=frozen,
            ) as resolver:
                parameters: dict = {}
                resolve_source_interface(
                    parameters,
                    configured,
                    executor_scope="edge-london-1",
                )

                self.assertEqual(parameters["source_ip"], "192.168.10.20")
                self.assertEqual(parameters["local_address"], "192.168.10.20/24")
                self.assertEqual(
                    parameters[SOURCE_INTERFACE_IDENTITY_KEY],
                    frozen.model_dump(mode="json"),
                )
                resolver.assert_called_once_with(
                    selection="default_route",
                    executor_scope="edge-london-1",
                )

    def test_configured_explicit_interface_freezes_actual_stable_identity(self) -> None:
        frozen = _identity(selection="explicit", source_ip="1.2.3.4", prefix_length=24)
        with patch(
            "app.services.engine_dispatch.interface_service.resolve_source_interface_identity",
            return_value=frozen,
        ) as resolver:
            parameters: dict = {}
            resolve_source_interface(
                parameters,
                "1.2.3.4/24",
                executor_scope="edge-london-1",
            )

        resolver.assert_called_once_with(
            selection="explicit",
            executor_scope="edge-london-1",
            source_ip="1.2.3.4",
            prefix_length=24,
        )
        self.assertEqual(parameters["source_ip"], "1.2.3.4")
        self.assertEqual(parameters["local_address"], "1.2.3.4/24")

    def test_bare_configured_ip_uses_the_nics_actual_prefix(self) -> None:
        frozen = _identity(selection="explicit", source_ip="1.2.3.4", prefix_length=16)
        with patch(
            "app.services.engine_dispatch.interface_service.resolve_source_interface_identity",
            return_value=frozen,
        ) as resolver:
            parameters: dict = {}
            resolve_source_interface(
                parameters,
                "1.2.3.4",
                executor_scope="edge-london-1",
            )

        resolver.assert_called_once_with(
            selection="explicit",
            executor_scope="edge-london-1",
            source_ip="1.2.3.4",
            prefix_length=None,
        )
        self.assertEqual(parameters["local_address"], "1.2.3.4/16")

    def test_run_level_source_fields_win_over_saved_configuration(self) -> None:
        frozen = _identity(selection="explicit", source_ip="10.0.0.9", prefix_length=8)
        parameters: dict = {
            "source_ip": "10.0.0.9",
            "local_address": "10.0.0.9/8",
            SOURCE_INTERFACE_IDENTITY_KEY: {"spoofed": True},
        }
        with patch(
            "app.services.engine_dispatch.interface_service.resolve_source_interface_identity",
            return_value=frozen,
        ) as resolver:
            resolve_source_interface(
                parameters,
                "1.2.3.4/24",
                executor_scope="edge-london-1",
            )

        resolver.assert_called_once_with(
            selection="explicit",
            executor_scope="edge-london-1",
            source_ip="10.0.0.9",
            prefix_length=8,
        )
        self.assertEqual(parameters[SOURCE_INTERFACE_IDENTITY_KEY], frozen.model_dump(mode="json"))

    def test_malformed_or_inconsistent_explicit_values_fail_before_inventory(self) -> None:
        for parameters, configured in (
            ({}, "not-an-ip"),
            ({}, "1.2.3.4/33"),
            ({"source_ip": "999.1.1.1"}, None),
            ({"source_ip": "1.2.3.4", "local_address": "1.2.4.4/24"}, None),
            ({"source_ip": "2001:db8::1"}, None),
        ):
            with self.subTest(parameters=parameters, configured=configured), patch(
                "app.services.engine_dispatch.interface_service.resolve_source_interface_identity"
            ) as resolver, self.assertRaises(ValueError):
                resolve_source_interface(
                    parameters,
                    configured,
                    executor_scope="edge-london-1",
                )
            resolver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
