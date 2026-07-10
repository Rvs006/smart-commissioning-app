"""Unit tests for engine_dispatch.resolve_bacnet_backend (real-backend default).

HONESTY: no sockets, no network, no bacpypes3. This exercises the pure
parameter-defaulting helper that makes an AUTHORIZED, non-dry-run BACnet run
select the real ``bacpypes3`` transport (so it ATTEMPTS real discovery) while
leaving dry-run previews and explicit operator/demo overrides untouched. The
consequences of that selection (a real Who-Is, or an honest failed run when
bacpypes3 is missing) live in the engine and are covered by the engine tests.
"""

import unittest

from app.services.engine_dispatch import resolve_bacnet_backend


class ResolveBacnetBackendTests(unittest.TestCase):
    def test_authorized_real_run_defaults_to_bacpypes3(self) -> None:
        # A real (non-dry-run) scan must attempt real discovery.
        parameters: dict = {"authorized": True}
        resolve_bacnet_backend(parameters)
        self.assertEqual(parameters["bacnet_backend"], "bacpypes3")

    def test_dry_run_is_left_untouched(self) -> None:
        # Dry-run previews stay on the engine default (simulated plan, no I/O).
        for value in (True, "true", "1", "yes", "on"):
            parameters: dict = {"dry_run": value}
            resolve_bacnet_backend(parameters)
            self.assertNotIn("bacnet_backend", parameters)

    def test_non_dry_run_rejects_explicit_simulated_backend(self) -> None:
        parameters: dict = {"authorized": True, "bacnet_backend": "simulated"}
        with self.assertRaisesRegex(ValueError, "only available for dry runs"):
            resolve_bacnet_backend(parameters)

    def test_explicit_bacpypes3_override_is_preserved(self) -> None:
        parameters: dict = {"authorized": True, "bacnet_backend": "bacpypes3"}
        resolve_bacnet_backend(parameters)
        self.assertEqual(parameters["bacnet_backend"], "bacpypes3")

    def test_unknown_backend_is_rejected(self) -> None:
        for selector in ("not-a-backend", False, 0):
            for dry_run in (False, True):
                parameters: dict = {
                    "authorized": True,
                    "bacnet_backend": selector,
                    "dry_run": dry_run,
                }
                with self.subTest(selector=selector, dry_run=dry_run):
                    with self.assertRaisesRegex(ValueError, "Unsupported BACnet backend"):
                        resolve_bacnet_backend(parameters)

    def test_no_other_parameters_are_added(self) -> None:
        # The helper only defaults the one key; it must not inject anything else.
        parameters: dict = {"authorized": True}
        resolve_bacnet_backend(parameters)
        self.assertEqual(parameters, {"authorized": True, "bacnet_backend": "bacpypes3"})


if __name__ == "__main__":
    unittest.main()
