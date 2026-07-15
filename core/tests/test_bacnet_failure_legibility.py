"""Every BACnet failure that used to be silent must now name itself.

Before v0.1.12 four different, common, real failures all produced the SAME
artifact — "succeeded, 0 devices, no error":

    * a contended UDP 47808 (a BACnet browser left open), because bacpypes3
      catches its own bind ``OSError`` and retries every second FOREVER;
    * this process's OWN leaked socket from a previous scan, because ``close()``
      had ZERO call sites, so the second scan of a session conflicted with itself;
    * a BBMD that refused a foreign-device registration, because ``BIPForeign``
      silently DROPS every broadcast while unregistered;
    * a BBMD that was never there at all.

Each is now a named, actionable, artifact-persisted outcome. These tests are the
only thing standing between that claim and a wish, so they assert the exact
sentence AND — where the engine self-diagnoses — the ABSENCE of base.py's
sanitized generic, which is what made every actionable message in this engine
invisible for the entire life of the feature.

WHAT THESE TESTS CANNOT PROVE — read this before trusting a green run:

    * NOT the BVLL wire behaviour. Not one BACnet frame is emitted here. The
      fakes model what bacpypes3's source SAYS it does (verified verbatim at the
      pinned 0.0.106). No test here shows that a Register-Foreign-Device PDU ever
      leaves the laptop, that it is well-formed, or that Forwarded-NPDUs return.
    * NOT whether a real BBMD accepts our registration. :class:`_FakeLinkLayer`
      returns the statuses the TEST scripts. Whether a real BBMD ever writes 0
      into ``bbmdRegistrationStatus`` — the single most likely way a live lab day
      goes wrong — is exactly what cannot be established from here. This suite
      proves that IF a BBMD refuses, the operator gets a sentence naming it and
      the BVLL result code. It does not make a refusal one percent less likely.
    * NOT the Windows errno mapping. CI is Linux. The 10048/10013 tests pin OUR
      classification of codes we believe Windows raises; the first execution
      against a real Windows socket is on the field laptop.
    * NOT that bacpypes3 accepts what ``build_transport_plan`` produces. The fake
      Application here accepts anything. ``test_bacpypes3_contract.py`` checks
      that surface against the real pinned package; only site validation checks
      the wire.

That gap is what on-site validation is for. A green suite here means "a failure
explains itself", never "BACnet discovery works".
"""

import asyncio
import errno
import socket
import sys
import types
import unittest
from typing import Any
from unittest import mock

from smart_commissioning_core.engines import bacnet_discovery
from smart_commissioning_core.engines.bacnet_discovery import (
    BACKEND_BACPYPES3,
    BIND_FAILED,
    BIND_PORT_IN_USE,
    FD_LOCAL_UDP_PORT,
    FD_REGISTRATION_PENDING,
    FD_REGISTRATION_REFUSED,
    FD_REGISTRATION_REGISTERED,
    FD_REGISTRATION_TIMEOUT,
    FD_REGISTRATION_UNKNOWN,
    ISSUE_EXPECTED_DEVICE_SILENT,
    LANE_BROADCAST,
    BacnetBindError,
    Bacpypes3Backend,
    SimulatedBacnetBackend,
    bind_error_message,
    build_empty_scan_hint,
    build_transport_plan,
    classify_bind_error,
    classify_fd_registration_status,
    preflight_bind,
    process_bacnet_discovery_run,
)
from smart_commissioning_core.engines.bacnet_params import (
    DEFAULT_FD_TTL,
    MODE_BROADCAST,
    MODE_FOREIGN_DEVICE,
    PARAM_BACNET_MODE,
    PARAM_BACNET_TARGETS,
    PARAM_BBMD_ADDRESS,
    BacnetTarget,
)
from smart_commissioning_core.engines.base import _SANITIZED_FAILURE_MESSAGE, ThrottleConfig

# THE SEAM RULE: run-parameter keys are imported from bacnet_params and spelled
# BY NAME, never as string literals — the same constants the backend's route
# tests import, so a key renamed on one side of the route <-> engine seam breaks
# both suites instead of neither.
#
# _SANITIZED_FAILURE_MESSAGE is imported private ON PURPOSE. Asserting the
# actionable message is present proves half of what matters; asserting the
# generic is ABSENT proves the other half, and that assertion has to name the
# real constant. A copy of the string here could drift out of sync with base.py
# and the test would keep passing while the operator saw nothing useful.

_AUTHORIZED: dict[str, Any] = {
    "scan_authorization": {"authorized": True, "authorized_by": "test.engineer@example.com"}
}

#: A Source Interface, so the engine's no-Source-Interface guard (which fires
#: before any transport work) is not what these tests accidentally exercise.
_INTERFACE = "192.168.1.10/24"


class FakeRunStore:
    """In-memory RunStore capturing what the run wrapper records.

    Deliberately a local copy rather than an import from test_bacnet_discovery:
    core/tests has no shared-helper module and no cross-test-import precedent,
    and coupling two suites through the alphabetical collection order to save 30
    lines would be a poor trade.
    """

    def __init__(self) -> None:
        self.summary_calls: list[dict[str, Any]] = []
        self.issues_calls: list[list[Any]] = []
        self.record_summary: dict[str, Any] = {}
        self.last_status: str | None = None
        self.last_error: str | None = None

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        self.last_status = status
        self.last_error = error_message
        return {
            "run_id": run_id,
            "status": status,
            "stage": stage,
            "progress_percent": progress_percent,
            "error_message": error_message,
            "result_summary": dict(self.record_summary),
        }

    def update_result_summary(
        self, run_id: str, result_summary: dict[str, Any], *, merge: bool = True
    ) -> dict[str, Any]:
        self.summary_calls.append(dict(result_summary))
        if merge:
            self.record_summary.update(result_summary)
        else:
            self.record_summary = dict(result_summary)
        return {"run_id": run_id, "result_summary": dict(self.record_summary)}

    def replace_issues(self, run_id: str, issues: list[Any]) -> dict[str, Any]:
        self.issues_calls.append(list(issues))
        return {"run_id": run_id, "issues": list(issues)}


class _RecordingBackend:
    """A scriptable stand-in for the real bacpypes3 transport.

    Labelled ``bacpypes3`` ON PURPOSE: the engine gates the empty-scan hint (and
    the Source-Interface guard) on the LIVE backend name, because a fixture's
    emptiness says nothing about a network. A simulated-labelled fake could not
    exercise either path.
    """

    def __init__(
        self,
        *,
        devices: list[dict[str, Any]] | None = None,
        directed: dict[str, list[dict[str, Any]]] | None = None,
        who_is_error: Exception | None = None,
        transport_plan: Any = None,
    ) -> None:
        self.backend_name = BACKEND_BACPYPES3
        self.transport_plan = transport_plan
        self._devices = [dict(row) for row in (devices or [])]
        self._directed = {
            address: [dict(row) for row in rows] for address, rows in (directed or {}).items()
        }
        self._who_is_error = who_is_error
        self.who_is_calls: list[tuple[int, int, str | None]] = []
        self.closed = 0
        # The diagnostic records the real backend populates and the engine stamps
        # into the run artifact from a finally.
        self.bind: dict[str, Any] | None = None
        self.fd_registration: dict[str, Any] | None = None

    async def who_is(
        self, low_limit: int, high_limit: int, address: str | None = None
    ) -> list[dict[str, Any]]:
        self.who_is_calls.append((low_limit, high_limit, address))
        if self._who_is_error is not None:
            raise self._who_is_error
        rows = self._devices if address is None else self._directed.get(address, [])
        return [dict(row) for row in rows if low_limit <= int(row["device_instance"]) <= high_limit]

    async def read_object_list(self, device: Any) -> list[dict[str, Any]]:
        return []

    async def read_present_value(self, device: Any, obj: Any) -> Any:
        return None

    def close(self) -> None:
        self.closed += 1


def _run(
    store: FakeRunStore,
    parameters: dict[str, Any],
    backend: Any,
    fd_backend: Any = None,
) -> tuple[Any, list[tuple[str, list[dict[str, Any]]]]]:
    """Drive one authorized, non-dry run through the public processor entrypoint."""
    persisted: list[tuple[str, list[dict[str, Any]]]] = []
    result = process_bacnet_discovery_run(
        "run_legibility",
        parameters,
        run_store=store,
        execution_mode="inline_local_fallback",
        backend=backend,
        fd_backend=fd_backend,
        throttle=ThrottleConfig(rate_limit_per_sec=None),
        persist_records=lambda run_id, records: persisted.append((run_id, list(records))),
    )
    return result, persisted


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _winsock_error(code: int) -> OSError:
    """An OSError reporting ``code`` through ``winerror``, the way Windows does.

    A CLASS attribute, not ``self.winerror = code``: on Windows ``OSError.winerror``
    is a read-only descriptor, so instance assignment would fail on the very
    platform this models. Subclassing shadows it on both platforms.

    And not ``OSError(10048, ...)``: on Linux CI that sets ``errno`` to 10048, a
    number that is NOT EADDRINUSE there — it would prove nothing about the field
    laptop's behaviour, while looking like it did.
    """
    return type("_WinsockOSError", (OSError,), {"winerror": code})("bind failed")


class BindErrorClassifierTests(unittest.TestCase):
    """The errno matrix. Defence in depth — see PortConflictLegibilityTests for the real mechanism."""

    def test_posix_port_in_use_codes_are_classified_as_a_port_conflict(self) -> None:
        for code in (errno.EADDRINUSE, errno.EACCES):
            with self.subTest(errno=code):
                self.assertEqual(classify_bind_error(OSError(code, "bind failed")), BIND_PORT_IN_USE)

    def test_windows_codes_arriving_through_winerror_are_classified_as_a_port_conflict(self) -> None:
        # 10048 WSAEADDRINUSE / 10013 WSAEACCES: the codes the field laptop will
        # actually raise. VERIFIED BY DESIGN, NEVER BY EXECUTION — this pins our
        # mapping, not Windows' behaviour (CI is Linux).
        for code in (10048, 10013):
            with self.subTest(winerror=code):
                self.assertEqual(classify_bind_error(_winsock_error(code)), BIND_PORT_IN_USE)

    def test_windows_codes_arriving_through_errno_are_also_matched(self) -> None:
        # A Windows socket OSError may surface the WSA code via errno, via
        # winerror, or both. The classifier checks the UNION of both attributes,
        # so it is correct under every mapping without platform branching.
        for code in (10048, 10013):
            with self.subTest(errno=code):
                self.assertEqual(classify_bind_error(OSError(code, "bind failed")), BIND_PORT_IN_USE)

    def test_an_unrelated_bind_failure_is_not_reported_as_a_port_conflict(self) -> None:
        # Telling an operator to "close your other BACnet tool" when the real
        # problem is a down interface sends them to the wrong place for an hour.
        for error in (OSError(errno.EADDRNOTAVAIL, "cannot assign"), _winsock_error(10065)):
            with self.subTest(error=error):
                self.assertEqual(classify_bind_error(error), BIND_FAILED)

    def test_bind_messages_name_the_port_the_interface_and_the_next_action(self) -> None:
        in_use = bind_error_message(BIND_PORT_IN_USE, ip="192.168.1.10", port=47808)
        self.assertIn("47808", in_use)
        self.assertIn("192.168.1.10", in_use)
        self.assertIn("BACnet browser", in_use)
        self.assertIn("Close it", in_use)

        failed = bind_error_message(BIND_FAILED, ip="192.168.1.10", port=47808, error_code=99)
        self.assertIn("Source Interface", failed)
        self.assertIn("(error 99)", failed)

        # Credential-free by construction: operator-configured values and numbers
        # only, never raw exception text.
        for message in (in_use, failed):
            self.assertNotIn(_SANITIZED_FAILURE_MESSAGE, message)


class PreflightBindTests(unittest.TestCase):
    """The pre-flight, against REAL sockets. The only mechanism that sees a conflict at all."""

    def test_a_free_port_passes_and_the_probe_socket_is_released(self) -> None:
        port = _free_udp_port()
        preflight_bind("127.0.0.1", port)
        # Called twice deliberately: the second call proves the first CLOSED its
        # probe. A pre-flight that leaked would report the port busy on the next
        # scan and blame a BACnet browser that was never running.
        preflight_bind("127.0.0.1", port)

    def test_a_real_holder_produces_an_actionable_port_conflict(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        port = int(holder.getsockname()[1])

        with self.assertRaises(BacnetBindError) as cm:
            preflight_bind("127.0.0.1", port)

        # The machine-readable kind is what lets the run record say
        # "udp_port_in_use" instead of only prose.
        self.assertEqual(cm.exception.kind, BIND_PORT_IN_USE)
        self.assertIn(str(port), str(cm.exception))
        self.assertIn("already in use", str(cm.exception))

    def test_the_bind_error_is_a_runtime_error(self) -> None:
        # BacnetBindError subclasses RuntimeError on purpose: RuntimeError is the
        # type the engine converts into a self-diagnosed failed run carrying the
        # message. Any other type is replaced by the sanitized generic.
        self.assertTrue(issubclass(BacnetBindError, RuntimeError))


class _FakeApplication:
    """Stands in for bacpypes3's Application. Never legitimately reached.

    ``from_object_list`` is the only entry point the backend uses, and the tests
    installing this fake all raise before it. If one ever gets here, that IS the
    failure under test: an Application was constructed despite a contended port,
    which is precisely the ordering bug the pre-flight exists to prevent.
    """

    @staticmethod
    def from_object_list(objects: Any) -> Any:
        raise AssertionError(
            "an Application must never be constructed after a failed bind pre-flight"
        )


class PortConflictLegibilityTests(unittest.TestCase):
    """The 47808 conflict, end to end, against a genuinely contended socket.

    The one test that drives the whole silence-killer together: a real bound UDP
    port, the real :class:`Bacpypes3Backend`, the real pre-flight, and the real
    engine — asserting the operator gets the actionable sentence rather than
    base.py's sanitized generic.

    bacpypes3 is faked in sys.modules only far enough to clear the install guard.
    The pre-flight raises before any bacpypes3 object is constructed, which is
    itself the ordering this pins.
    """

    def setUp(self) -> None:
        # Registered FIRST so it runs LAST (addCleanup is LIFO) — i.e. after the
        # patch is undone. Cleanup is not optional here: a leaked fake would make
        # the absence-guard tests in test_bacnet_discovery.py skip SILENTLY, and
        # collection is alphabetical, so this file runs before them.
        self.addCleanup(self._assert_the_fake_was_removed)

        self._package = types.ModuleType("bacpypes3")
        app_module = types.ModuleType("bacpypes3.app")
        app_module.Application = _FakeApplication
        self._package.app = app_module
        patcher = mock.patch.dict(
            sys.modules, {"bacpypes3": self._package, "bacpypes3.app": app_module}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_the_fake_was_removed(self) -> None:
        self.assertIsNot(
            sys.modules.get("bacpypes3"),
            self._package,
            "the fake bacpypes3 leaked into sys.modules; the import-guard tests would silently skip",
        )

    def test_a_contended_udp_port_fails_the_run_with_a_sentence_the_operator_can_act_on(
        self,
    ) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        port = int(holder.getsockname()[1])

        store = FakeRunStore()
        result, persisted = _run(
            store,
            {
                **_AUTHORIZED,
                "bacnet_backend": BACKEND_BACPYPES3,
                "local_address": f"127.0.0.1:{port}",
            },
            backend=None,  # no injection: build and drive the REAL backend
        )

        self.assertEqual(result["status"], "failed")
        message = str(result["error_message"])
        # This exact scenario used to be indistinguishable from a quiet network:
        # bacpypes3 swallows the bind OSError in an immortal retry and who_is just
        # returns [].
        self.assertIn(str(port), message)
        self.assertIn("already in use", message)
        self.assertIn("Close it", message)
        # ...and it must NOT be replaced by the sanitized generic, which is what
        # made every actionable message this engine raises invisible.
        self.assertNotIn(_SANITIZED_FAILURE_MESSAGE, message)
        self.assertEqual(store.last_error, message)
        self.assertEqual(persisted, [], "a failed transport must persist no devices")

    def test_a_failed_bind_still_diagnoses_itself_from_the_run_record_alone(self) -> None:
        # The v0.1.12 bar: there is no live debugging session on a lab floor, so a
        # failed scan must be reconstructible from GET /discovery/runs/{id} alone.
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        port = int(holder.getsockname()[1])

        store = FakeRunStore()
        _run(
            store,
            {
                **_AUTHORIZED,
                "bacnet_backend": BACKEND_BACPYPES3,
                "local_address": f"127.0.0.1:{port}",
            },
            backend=None,
        )

        diagnostics = store.summary_calls[-1]["bacnet_diagnostics"]
        self.assertEqual(
            diagnostics["bind"],
            {
                "attempted": True,
                "ok": False,
                "ip": "127.0.0.1",
                "port": port,
                "reason": BIND_PORT_IN_USE,
            },
        )
        self.assertEqual(diagnostics["udp_port"], port)
        # The interface is recorded verbatim as the operator typed it, so a
        # mis-parsed suffix is visible rather than inferred.
        self.assertEqual(diagnostics["interface"], f"127.0.0.1:{port}")
        self.assertEqual(diagnostics["mode"], MODE_BROADCAST)
        # An UNVERIFIED transport is never a clean empty scan, so no hint claims
        # the network was quiet.
        self.assertFalse(diagnostics["transport_verified"])
        self.assertNotIn("empty_scan_hint", store.summary_calls[-1])


class SocketLifecycleTests(unittest.TestCase):
    """close() must actually run — on the success path AND on every failure path.

    Until v0.1.12 ``close()`` had ZERO call sites. The portable exe runs engines
    inline in one long-lived process, so the first live scan's bound UDP 47808
    socket leaked for the life of the app and the SECOND scan of the session
    conflicted with itself — invisibly, because bacpypes3 swallows bind failures
    in an immortal retry. Every scan after the first silently returned 0 devices.
    Nobody ever reported it, because it does not look like a bug.
    """

    def test_close_runs_on_the_success_path(self) -> None:
        store = FakeRunStore()
        backend = _RecordingBackend(devices=[{"device_instance": 1001, "address": "10.0.0.11:47808"}])
        result, _ = _run(store, {**_AUTHORIZED, "local_address": _INTERFACE}, backend)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(backend.closed, 1)

    def test_close_runs_on_the_failure_path_too(self) -> None:
        # The leak is WORSE on failure: an un-closed socket makes the NEXT scan's
        # pre-flight blame "another BACnet tool" for a port this very process is
        # holding. close()-in-a-finally is what keeps that message true.
        store = FakeRunStore()
        backend = _RecordingBackend(who_is_error=RuntimeError("UDP port 47808 is already in use"))
        result, _ = _run(store, {**_AUTHORIZED, "local_address": _INTERFACE}, backend)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(backend.closed, 1)

    def test_close_runs_for_both_apps_of_a_foreign_device_run(self) -> None:
        store = FakeRunStore()
        backend = _RecordingBackend(devices=[])
        fd_backend = _RecordingBackend(devices=[])
        _run(
            store,
            {
                **_AUTHORIZED,
                "local_address": _INTERFACE,
                PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
                PARAM_BBMD_ADDRESS: "10.0.0.5",
            },
            backend,
            fd_backend=fd_backend,
        )

        self.assertEqual(backend.closed, 1)
        self.assertEqual(fd_backend.closed, 1, "lane 3 binds its own socket and must release it")

    def test_an_unvetted_exception_is_still_sanitized_and_still_closes(self) -> None:
        # Only RuntimeError is treated as vetted-for-the-operator. Anything else
        # still goes to base.py's sanitizer, because only the vetted messages have
        # been checked for credential leakage — and the socket is released either
        # way.
        store = FakeRunStore()
        backend = _RecordingBackend(who_is_error=ValueError("raw transport detail s3cret-token"))
        result, _ = _run(store, {**_AUTHORIZED, "local_address": _INTERFACE}, backend)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.last_error, _SANITIZED_FAILURE_MESSAGE)
        self.assertNotIn("s3cret-token", str(store.last_error))
        self.assertEqual(backend.closed, 1)


class _FakeLinkLayer:
    """Stands in for bacpypes3's BIPForeign.

    ``bbmdRegistrationStatus`` is the ONLY signal that a BBMD refused us — a
    refusal is otherwise identical to an empty network. Verified verbatim against
    bacpypes3 ipv4/service.py: initialised to -2, with the comment
    ``# -2=unregistered, -1=in process, 0=OK, >0 error``, and the Result-LPDU
    handler assigns ``lpdu.bvlciResultCode`` into it.

    The script is a queue of statuses: each read returns the next one, and the
    last value repeats forever. That models the real asynchronous transition
    (-2 -> -1 -> 0) that the engine's bounded poll exists to wait out.
    """

    def __init__(self, statuses: list[Any]) -> None:
        self._statuses = list(statuses)
        self.reads = 0

    @property
    def bbmdRegistrationStatus(self) -> Any:
        self.reads += 1
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]


class _FakeApp:
    def __init__(self, link_layers: dict[Any, Any]) -> None:
        self.link_layers = link_layers


class _FakeNetworkPort:
    def __init__(self, object_id: Any) -> None:
        self.objectIdentifier = object_id


class ForeignDeviceRegistrationStatusTests(unittest.TestCase):
    """The PURE status classifier — the BBMD's answer, decoded."""

    def test_zero_is_the_only_value_that_means_registered(self) -> None:
        self.assertEqual(classify_fd_registration_status(0), FD_REGISTRATION_REGISTERED)

    def test_any_positive_value_is_a_refusal_and_carries_the_bvll_result_code(self) -> None:
        for status in (1, 48, 65535):
            with self.subTest(status=status):
                self.assertEqual(classify_fd_registration_status(status), FD_REGISTRATION_REFUSED)

    def test_negative_values_are_still_pending(self) -> None:
        for status in (-1, -2):  # in process / unregistered
            with self.subTest(status=status):
                self.assertEqual(classify_fd_registration_status(status), FD_REGISTRATION_PENDING)

    def test_a_non_integer_status_is_unknown_rather_than_assumed(self) -> None:
        # True must NOT read as a positive result code: manufacturing a "refused"
        # out of a type error would send an operator to their BBMD administrator
        # over a bug in this file.
        for status in (True, False, None, "0", 1.5, object()):
            with self.subTest(status=status):
                self.assertEqual(classify_fd_registration_status(status), FD_REGISTRATION_UNKNOWN)


class ForeignDeviceRegistrationWaitTests(unittest.TestCase):
    """The bounded wait on the BBMD's answer, against scripted fakes.

    Reaching into ``_app`` / ``_network_port_object`` is deliberate: it is the
    seam that lets CI exercise the BBMD paths with no BBMD. The only other way to
    populate them is ``_ensure_app``, which requires bacpypes3 and a real socket.
    """

    def _backend(
        self, statuses: list[Any], *, bbmd: str = "10.0.0.5"
    ) -> tuple[Bacpypes3Backend, _FakeLinkLayer]:
        backend = Bacpypes3Backend(
            local_address=_INTERFACE,
            parameters={PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE, PARAM_BBMD_ADDRESS: bbmd},
            mode=MODE_FOREIGN_DEVICE,
            udp_port=FD_LOCAL_UDP_PORT,
        )
        link_layer = _FakeLinkLayer(statuses)
        object_id = ("network-port", 1)
        backend._app = _FakeApp({object_id: link_layer})
        backend._network_port_object = _FakeNetworkPort(object_id)
        return backend, link_layer

    def test_an_acknowledged_registration_records_what_it_registered_against(self) -> None:
        backend, _ = self._backend([0])
        record = asyncio.run(backend._wait_for_fd_registration())

        self.assertEqual(record["outcome"], FD_REGISTRATION_REGISTERED)
        self.assertEqual(record["status"], 0)
        self.assertEqual(record["mode"], MODE_FOREIGN_DEVICE)
        # The RESOLVED "ip:port" handed to HostNPort — deliberately not named
        # bbmd_address, which is the bare-IP parameter. Two different shapes under
        # one name is how someone reading them side by side concludes the config
        # was mangled.
        self.assertEqual(record["fd_bbmd_address"], "10.0.0.5:47808")
        self.assertEqual(record["fd_ttl"], DEFAULT_FD_TTL)
        self.assertEqual(record["local_udp_port"], FD_LOCAL_UDP_PORT)
        self.assertEqual(backend.fd_registration, record)

    def test_the_registration_is_waited_for_not_assumed(self) -> None:
        # bacpypes3 registers asynchronously at construction, so the status is -2
        # then -1 for a while. Reading it once and scanning would race the BBMD's
        # answer — and an unregistered BIPForeign silently DROPS every broadcast,
        # so losing that race looks exactly like an empty network.
        backend, link_layer = self._backend([-2, -1, -1, 0])
        with mock.patch.object(bacnet_discovery, "FD_REGISTRATION_POLL_INTERVAL_S", 0.001):
            record = asyncio.run(backend._wait_for_fd_registration())

        self.assertEqual(record["outcome"], FD_REGISTRATION_REGISTERED)
        self.assertGreaterEqual(link_layer.reads, 4, "the poll must outlast a -1 'in process'")

    def test_a_refused_registration_names_the_bbmd_and_the_bvll_result_code(self) -> None:
        # The most likely way a live lab day goes wrong: many BBMDs run locked
        # foreign-device tables. Until v0.1.12 this was indistinguishable from an
        # empty network, and it is the one failure the code cannot fix — only name.
        backend, _ = self._backend([48])

        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(backend._wait_for_fd_registration())

        message = str(cm.exception)
        self.assertIn("refused", message)
        self.assertIn("10.0.0.5:47808", message)
        # The exact code the BBMD sent back — the difference between "BACnet
        # didn't work" and something the site's BBMD administrator can act on.
        self.assertIn("BVLL result code 48", message)
        self.assertIn("administrator", message)
        self.assertNotIn(_SANITIZED_FAILURE_MESSAGE, message)
        # The record survives the raise, so a failed run still explains itself.
        self.assertEqual(backend.fd_registration["outcome"], FD_REGISTRATION_REFUSED)
        self.assertEqual(backend.fd_registration["status"], 48)

    def test_a_bbmd_that_never_answers_times_out_and_never_falls_back(self) -> None:
        # Stuck at -1: not a BBMD that said no, a BBMD that never spoke. The two
        # need different messages because they send the operator to different
        # places (BBMD policy vs address/routing/firewall).
        backend, _ = self._backend([-1])
        with (
            mock.patch.object(bacnet_discovery, "FD_REGISTRATION_WAIT_S", 0.05),
            mock.patch.object(bacnet_discovery, "FD_REGISTRATION_POLL_INTERVAL_S", 0.001),
        ):
            with self.assertRaises(RuntimeError) as cm:
                asyncio.run(backend._wait_for_fd_registration())

        message = str(cm.exception)
        self.assertIn("No response from the BBMD", message)
        self.assertIn("10.0.0.5:47808", message)
        self.assertIn("UDP", message)
        self.assertEqual(backend.fd_registration["outcome"], FD_REGISTRATION_TIMEOUT)
        self.assertEqual(backend.fd_registration["status"], -1)

    def test_an_unreadable_status_stops_the_scan_rather_than_assuming_success(self) -> None:
        # bacpypes3's internals moved (wrong version installed). Scanning anyway
        # would report results from an unverified BBMD registration — a clean-
        # looking empty that means nothing.
        backend = Bacpypes3Backend(
            local_address=_INTERFACE,
            parameters={PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE, PARAM_BBMD_ADDRESS: "10.0.0.5"},
            mode=MODE_FOREIGN_DEVICE,
            udp_port=FD_LOCAL_UDP_PORT,
        )
        backend._app = _FakeApp({})
        backend._network_port_object = _FakeNetworkPort(("network-port", 1))

        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(backend._wait_for_fd_registration())

        message = str(cm.exception)
        self.assertIn("bacpypes3==0.0.106", message)
        self.assertIn("10.0.0.5:47808", message)
        self.assertEqual(backend.fd_registration["outcome"], FD_REGISTRATION_UNKNOWN)

    def test_a_broadcast_app_never_waits_for_a_bbmd(self) -> None:
        # The zero-regression pin for the registration gate: a plain local scan
        # must not acquire a BBMD dependency it never had.
        backend = Bacpypes3Backend(local_address=_INTERFACE, parameters={}, mode=MODE_BROADCAST)
        asyncio.run(backend._ensure_registered())
        self.assertIsNone(backend.fd_registration)


class BbmdRefusalFailsTheRunTests(unittest.TestCase):
    """A refused BBMD hard-fails the run. It NEVER silently becomes a broadcast scan."""

    def _fd_plan(self) -> Any:
        return build_transport_plan(
            {
                "local_address": _INTERFACE,
                PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
                PARAM_BBMD_ADDRESS: "10.0.0.5",
            },
            udp_port=FD_LOCAL_UDP_PORT,
        )

    def test_a_bbmd_refusal_fails_the_whole_run_and_never_falls_back_to_broadcast(self) -> None:
        # Lane 1 DID hear a device here. Reporting that as a successful scan would
        # tell the operator "your BACnet network has 1 device" at the moment the
        # BBMD they asked to reach the other 59 through just said no. Quietly
        # continuing on local broadcast is the original bug wearing a new hat.
        store = FakeRunStore()
        backend = _RecordingBackend(devices=[{"device_instance": 1001, "address": "10.10.0.11:47808"}])
        fd_backend = _RecordingBackend(
            who_is_error=RuntimeError(
                "The BBMD at 10.0.0.5:47808 refused foreign-device registration "
                "(BVLL result code 48). Ask the BBMD administrator to permit "
                "foreign-device registrations from this machine's IP address."
            ),
            transport_plan=self._fd_plan(),
        )
        fd_backend.fd_registration = {
            "outcome": FD_REGISTRATION_REFUSED,
            "status": 48,
            "fd_bbmd_address": "10.0.0.5:47808",
        }

        result, persisted = _run(
            store,
            {
                **_AUTHORIZED,
                "local_address": _INTERFACE,
                PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
                PARAM_BBMD_ADDRESS: "10.0.0.5",
            },
            backend,
            fd_backend=fd_backend,
        )

        self.assertEqual(result["status"], "failed")
        message = str(result["error_message"])
        self.assertIn("refused foreign-device registration", message)
        self.assertIn("10.0.0.5:47808", message)
        self.assertIn("BVLL result code 48", message)
        self.assertNotIn(_SANITIZED_FAILURE_MESSAGE, message)
        self.assertEqual(store.last_error, message)
        # Lane 1's device is NOT reported: a lane that failed is not replaced by a
        # lane that worked.
        self.assertEqual(persisted, [])

        summary = store.summary_calls[-1]
        # ...but the diagnostics still say exactly which leg failed and what the
        # BBMD said, from the artifact alone.
        self.assertEqual(summary["bacnet_diagnostics"]["fd_registration"]["outcome"], FD_REGISTRATION_REFUSED)
        self.assertEqual(summary["bacnet_diagnostics"]["fd_registration"]["status"], 48)
        self.assertFalse(summary["bacnet_diagnostics"]["transport_verified"])
        self.assertTrue(summary["lanes"][LANE_BROADCAST]["ran"])
        # Both apps still released their sockets on the failure path.
        self.assertEqual(backend.closed, 1)
        self.assertEqual(fd_backend.closed, 1)


class EmptyScanHintTests(unittest.TestCase):
    """Finding nothing is a VALID result — it just never goes out unexplained again."""

    def test_the_broadcast_hint_names_the_interface_the_window_and_the_next_thing_to_try(
        self,
    ) -> None:
        hint = build_empty_scan_hint(
            mode=MODE_BROADCAST,
            interface=_INTERFACE,
            instance_low=0,
            instance_high=4194303,
            timeout_s=5.0,
        )
        self.assertIn("No devices answered the local broadcast", hint)
        self.assertIn(_INTERFACE, hint)
        self.assertIn("0–4194303", hint)
        # The single most useful next action for the most common cause: the
        # devices are on another subnet behind a BBMD.
        self.assertIn("Foreign Device", hint)

    def test_the_foreign_device_hint_points_at_the_bbmd_not_the_local_interface(self) -> None:
        # Once registration succeeded, telling the operator to check their local
        # adapter would send them to the wrong place: the question is now whether
        # the BBMD's distribution table covers the devices' subnets.
        hint = build_empty_scan_hint(
            mode=MODE_FOREIGN_DEVICE,
            interface=_INTERFACE,
            instance_low=0,
            instance_high=100,
            timeout_s=5.0,
            fd_bbmd_address="10.0.0.5:47808",
        )
        self.assertIn("Registered with the BBMD at 10.0.0.5:47808", hint)
        self.assertIn("broadcast distribution table", hint)

    def test_the_hint_reports_unanswered_directed_probes(self) -> None:
        many = build_empty_scan_hint(
            mode=MODE_BROADCAST,
            interface=_INTERFACE,
            instance_low=0,
            instance_high=1,
            timeout_s=5.0,
            unanswered_directed=3,
        )
        self.assertIn("3 directed Who-Is to register addresses", many)

        one = build_empty_scan_hint(
            mode=MODE_BROADCAST,
            interface=_INTERFACE,
            instance_low=0,
            instance_high=1,
            timeout_s=5.0,
            unanswered_directed=1,
        )
        self.assertIn("1 directed Who-Is to register address from", one)

    def test_a_live_empty_scan_stays_succeeded_but_explains_itself(self) -> None:
        store = FakeRunStore()
        backend = _RecordingBackend(devices=[])
        result, _ = _run(store, {**_AUTHORIZED, "local_address": _INTERFACE}, backend)

        # Finding nothing is a real answer, so the status must NOT become a
        # failure. But an unexplained zero is what started all of this.
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["device_count"], 0)
        self.assertIn("No devices answered the local broadcast", summary["empty_scan_hint"])
        # A CLEAN empty is only claimable because the transport was affirmatively
        # verified first — everything that could have made it a lie raises earlier.
        self.assertTrue(summary["bacnet_diagnostics"]["transport_verified"])

    def test_an_empty_foreign_device_scan_names_the_bbmd_it_registered_with(self) -> None:
        store = FakeRunStore()
        plan = build_transport_plan(
            {
                "local_address": _INTERFACE,
                PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
                PARAM_BBMD_ADDRESS: "10.0.0.5",
            },
            udp_port=FD_LOCAL_UDP_PORT,
        )
        backend = _RecordingBackend(devices=[])
        fd_backend = _RecordingBackend(devices=[], transport_plan=plan)
        result, _ = _run(
            store,
            {
                **_AUTHORIZED,
                "local_address": _INTERFACE,
                PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
                PARAM_BBMD_ADDRESS: "10.0.0.5",
            },
            backend,
            fd_backend=fd_backend,
        )

        self.assertEqual(result["status"], "succeeded")
        hint = store.summary_calls[-1]["empty_scan_hint"]
        self.assertIn("Registered with the BBMD at 10.0.0.5:47808", hint)

    def test_no_hint_is_stamped_when_devices_answered(self) -> None:
        store = FakeRunStore()
        backend = _RecordingBackend(devices=[{"device_instance": 1001, "address": "10.0.0.11:47808"}])
        _run(store, {**_AUTHORIZED, "local_address": _INTERFACE}, backend)

        self.assertNotIn("empty_scan_hint", store.summary_calls[-1])

    def test_a_simulated_empty_scan_gets_no_hint(self) -> None:
        # A fixture's emptiness says NOTHING about a network. Explaining it as if
        # it did would be fabricating a diagnosis — and the fixture is what dry-run
        # previews use.
        store = FakeRunStore()
        backend = SimulatedBacnetBackend()
        _run(
            store,
            {
                **_AUTHORIZED,
                "local_address": _INTERFACE,
                "device_instance_low": 3000,
                "device_instance_high": 3001,  # no fixture device lives here
            },
            backend,
        )

        summary = store.summary_calls[-1]
        self.assertEqual(summary["device_count"], 0)
        self.assertNotIn("empty_scan_hint", summary)


class ExpectedButSilentTests(unittest.TestCase):
    """A register row nothing answered is AMBER and INCONCLUSIVE — never "device absent"."""

    def test_a_silent_register_row_is_reported_without_failing_the_run(self) -> None:
        store = FakeRunStore()
        backend = _RecordingBackend(devices=[{"device_instance": 1001, "address": "10.10.0.11:47808"}])
        heard = BacnetTarget(address="10.10.0.11", device_instance=1001, asset_id="AHU-1")
        silent = BacnetTarget(
            address="10.10.0.99",
            device_instance=9001,
            asset_id="VAV-9",
            asset_name="VAV-9 third floor",
        )
        result, _ = _run(
            store,
            {
                **_AUTHORIZED,
                "local_address": _INTERFACE,
                PARAM_BACNET_TARGETS: [heard.as_dict(), silent.as_dict()],
            },
            backend,
        )

        # AMBER, never a failure. BACnet-135 lets a device answer a directed
        # Who-Is with a local-broadcast I-Am this host cannot hear from another
        # subnet, and routed MS/TP devices are invisible to the unicast lane BY
        # DESIGN. Failing the run on this would fail a healthy lab.
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["expected_device_count"], 2)
        self.assertEqual(summary["expected_responding_count"], 1)
        self.assertEqual(
            summary["expected_not_responding"],
            [
                {
                    "asset_id": "VAV-9",
                    "asset_name": "VAV-9 third floor",
                    "device_instance": 9001,
                    "address": "10.10.0.99",
                    "directed_probe_sent": True,
                }
            ],
        )

    def test_the_silent_device_issue_never_claims_the_device_is_offline(self) -> None:
        store = FakeRunStore()
        backend = _RecordingBackend(devices=[])
        silent = BacnetTarget(
            address="10.10.0.99",
            device_instance=9001,
            asset_id="VAV-9",
            asset_name="VAV-9 third floor",
        )
        _run(
            store,
            {
                **_AUTHORIZED,
                "local_address": _INTERFACE,
                PARAM_BACNET_TARGETS: [silent.as_dict()],
            },
            backend,
        )

        issues = [i for i in store.issues_calls[-1] if i.issue_type == ISSUE_EXPECTED_DEVICE_SILENT]
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue.asset_id, "VAV-9")
        # Medium, deliberately: escalating an inconclusive finding trains an
        # operator to ignore the colour.
        self.assertEqual(issue.severity, "medium")
        self.assertIn("VAV-9 third floor", issue.description)
        self.assertIn("10.10.0.99", issue.description)
        # THE wording pin. Putting "device offline" next to dozens of lab devices
        # that are merely unreachable by one lane would send an operator hunting
        # faults that do not exist.
        self.assertIn("INCONCLUSIVE", issue.description)
        self.assertIn("not proof the device is offline", issue.description)
        self.assertIn("no answer to a directed Who-Is sent to 10.10.0.99", issue.description)


if __name__ == "__main__":
    unittest.main()
