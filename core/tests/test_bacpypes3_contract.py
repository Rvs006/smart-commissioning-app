"""API-surface contract tests against the REAL, pinned bacpypes3.

WHY THIS FILE EXISTS
====================

Every other test of the BACnet lane mocks bacpypes3 away. That is deliberate and
correct — the `python` CI job never installs the ``[bacnet]`` extra, and the
import-guard tests in test_bacnet_discovery.py only pass *because* it is absent.
But it leaves exactly one class of failure completely uncaught: our code naming
an API that the pinned bacpypes3 does not actually have.

That failure is invisible here and fatal on site. Every bacpypes3 import in the
engine is function-local and lazy, so a wrong module path raises nothing until a
real, authorized run on a real network. The fakes answer to whatever names we
invent; a real device does not. This suite is the ONLY pre-lab mechanism that
asks the actual pinned package whether our assumptions are true.

It is run by the non-blocking ``bacpypes3-contract`` job in
.github/workflows/ci.yml, which installs ``core[bacnet]``. In the main `python`
job bacpypes3 is absent and every test here skips.

A FAILURE HERE IS NOT FLAKINESS. bacpypes3 is pre-1.0 with a moving API and is
EXACT-PINNED in core/pyproject.toml; the portable exe shipped to site freezes
that same pin. Red here means the pinned API moved out from under the
foreign-device code and the field build is at risk.


WHAT THIS CANNOT PROVE
======================

Introspection is not execution. Nothing here proves that bacpypes3 accepts our
constructed stack, that it emits Register-Foreign-Device on the wire, that a
BBMD answers it, or that a device replies to a directed Who-Is. Those are first
proven against real hardware. This suite only proves that the symbols we build
all of that out of still exist, with the shapes we call them by.
"""

import ast
import importlib
import importlib.metadata
import importlib.util
import inspect
import unittest
from typing import Any

_INSTALLED = importlib.util.find_spec("bacpypes3") is not None
_SKIP_REASON = "bacpypes3 is not installed; run the bacpypes3-contract CI job (installs core[bacnet])"

#: The engine whose bacpypes3 imports this suite validates.
_ENGINE_MODULE = "smart_commissioning_core.engines.bacnet_discovery"

# Where each symbol is expected to live. These are only FALLBACKS: when the
# engine imports the symbol itself, _resolve() follows the engine's own import
# instead, so this file checks the module our code really uses rather than the
# module we once assumed it used.
_APPLICATION_MODULE = "bacpypes3.app"
_NETWORK_PORT_MODULE = "bacpypes3.local.networkport"
_BASETYPES_MODULE = "bacpypes3.basetypes"
_BIP_FOREIGN_MODULE = "bacpypes3.ipv4.service"

#: The exact keyword arguments the engine calls Application.who_is with.
_WHO_IS_PARAMETERS = ("low_limit", "high_limit", "address", "timeout")

#: The three NetworkPortObject properties that make a stack a foreign device.
_FOREIGN_DEVICE_PROPERTIES = ("bacnetIPMode", "fdBBMDAddress", "fdSubscriptionLifetime")


def _pinned_version() -> str:
    try:
        return importlib.metadata.version("bacpypes3")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - job installs it
        return "unknown"


def _moved(what: str) -> str:
    """Wrap a failure in what it actually means, for whoever reads the red job."""
    return (
        f"{what}\n\n"
        f"THE PINNED bacpypes3 API MOVED (installed: {_pinned_version()}). This is not a "
        "flaky test and not a mock drifting — it is the real package disagreeing with the "
        "BACnet foreign-device code. core/pyproject.toml exact-pins bacpypes3 and the "
        "portable exe that goes to site freezes that same pin, so the field build is at "
        "risk. Re-verify the symbol against the installed source, then either fix the "
        "engine or move the pin deliberately."
    )


def _is_bacpypes3(module: str | None) -> bool:
    return bool(module) and (module == "bacpypes3" or str(module).startswith("bacpypes3."))


def _engine_bacpypes3_imports() -> list[tuple[str, str]]:
    """Every bacpypes3 import the engine actually writes, as ``(module, name)``.

    Read out of the engine's AST rather than hardcoded here, so this file cannot
    drift from the code it protects. If the engine starts importing ``HostNPort``
    from a different module, these tests follow it there and check THAT module
    against the real package. ``name`` is "" for a plain ``import bacpypes3.x``.
    """
    tree = ast.parse(inspect.getsource(importlib.import_module(_ENGINE_MODULE)))
    imports: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, which can never be bacpypes3.
            if not node.level and _is_bacpypes3(node.module):
                imports.extend((str(node.module), alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            imports.extend((alias.name, "") for alias in node.names if _is_bacpypes3(alias.name))
    return imports


def _provides(module_name: str, name: str) -> bool:
    """Does the real package expose ``name`` at ``module_name``?"""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    return not name or name == "*" or hasattr(module, name)


def _engine_import_candidates(name: str) -> list[str]:
    """Modules the engine imports ``name`` from, in source order.

    More than one means the engine deliberately tries alternatives (e.g. a
    ``try: from A import X / except ImportError: from B import X`` chain, a
    reasonable hedge while a symbol's home module is unconfirmed). Callers must
    treat that as "at least one must work", never "all must work" — failing on
    the unused branch of a working fallback would be a false alarm, and a loud
    job that cries wolf is worse than no job.
    """
    return [module for module, imported in _engine_bacpypes3_imports() if imported == name]


def _resolve(name: str, default_module: str) -> tuple[str, Any]:
    """Import ``name`` from the module the ENGINE imports it from, else ``default_module``.

    Returns the first candidate module that actually provides the symbol, so a
    fallback chain resolves the same way the engine's own imports would.
    """
    candidates = _engine_import_candidates(name) or [default_module]
    for module_name in candidates:
        if _provides(module_name, name):
            return module_name, getattr(importlib.import_module(module_name), name)
    return " or ".join(candidates), None


def _declared_names(cls: type) -> set[str]:
    """Every property name a bacpypes3 object class exposes, from all angles.

    bacpypes3 declares object properties as class ANNOTATIONS (its metaclass
    turns them into elements), so ``hasattr`` alone can miss a property that is
    genuinely there. Union the annotations across the MRO, any ``_elements``
    registry, and ``dir()`` — a name found by any of them is present.
    """
    names: set[str] = set(dir(cls))
    for klass in inspect.getmro(cls):
        namespace = vars(klass)
        annotations = namespace.get("__annotations__")
        if isinstance(annotations, dict):
            names.update(annotations)
        elements = namespace.get("_elements")
        if isinstance(elements, dict):
            names.update(elements)
        elif isinstance(elements, (list, tuple, set, frozenset)):
            names.update(str(getattr(element, "name", element)) for element in elements)
    return names


@unittest.skipUnless(_INSTALLED, _SKIP_REASON)
class EngineImportContractTests(unittest.TestCase):
    """Every bacpypes3 name the engine imports must exist in the pinned package."""

    def test_every_bacpypes3_import_in_the_engine_resolves(self) -> None:
        # This is the general net under the named tests below. The engine's
        # bacpypes3 imports are all function-local and lazy, so a wrong module
        # path raises nothing until a real run on a real network — no other test
        # in this repo executes those import statements at all.
        imports = _engine_bacpypes3_imports()
        self.assertNotEqual(
            imports,
            [],
            f"{_ENGINE_MODULE} imports no bacpypes3 symbols at all. Either the real transport "
            "was removed, or this test lost its anchor and is now checking nothing.",
        )
        # Group by imported symbol: several modules for one symbol is a
        # deliberate fallback chain, where only one of them has to resolve.
        groups: dict[str, list[tuple[str, str]]] = {}
        for module_name, name in imports:
            groups.setdefault(name or f"import {module_name}", []).append((module_name, name))

        for symbol, candidates in groups.items():
            with self.subTest(symbol=symbol):
                where = " or ".join(f"`{module_name}`" for module_name, _ in candidates)
                self.assertNotEqual(
                    [module_name for module_name, name in candidates if _provides(module_name, name)],
                    [],
                    _moved(
                        f"{_ENGINE_MODULE} imports `{symbol}` from {where}. The pinned bacpypes3 "
                        "does not provide it there. Because that import is lazy and "
                        "function-local, nothing else in CI executes it — it would first raise on "
                        "a real, authorized run against real hardware."
                    ),
                )


@unittest.skipUnless(_INSTALLED, _SKIP_REASON)
class ApplicationContractTests(unittest.TestCase):
    """The Application surface the three discovery lanes are built on."""

    def test_who_is_still_accepts_a_directed_address(self) -> None:
        # The entire unicast lane (Lane 2) is who_is(low, high, address=Address(ip)).
        # v0.1.12 deletes an in-code comment claiming this parameter does not
        # exist; this test is what keeps that deletion honest against the real
        # package instead of against a fake that answers to anything.
        module_name, application = _resolve("Application", _APPLICATION_MODULE)
        self.assertIsNotNone(application, _moved(f"Application is not importable from {module_name}."))
        who_is = getattr(application, "who_is", None)
        self.assertIsNotNone(
            who_is,
            _moved(f"{module_name}.Application has no who_is — every discovery lane calls it."),
        )
        signature = inspect.signature(who_is)
        missing = [name for name in _WHO_IS_PARAMETERS if name not in signature.parameters]
        self.assertEqual(
            missing,
            [],
            _moved(
                f"Application.who_is{signature} no longer accepts {missing}. The directed-unicast "
                "lane passes address= to reach register devices that local broadcast cannot; "
                "without it that lane cannot exist."
            ),
        )
        for name in _WHO_IS_PARAMETERS:
            with self.subTest(parameter=name):
                kind = signature.parameters[name].kind
                self.assertIn(
                    kind,
                    (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY),
                    _moved(f"who_is' `{name}` is now {kind} — the engine passes it by keyword."),
                )
        self.assertIsNone(
            signature.parameters["address"].default,
            _moved(
                "who_is(address=...) no longer defaults to None. Lane 1 — plain local broadcast, "
                "the path that works today — relies on omitting address meaning global broadcast. "
                "A changed default silently redirects the one lane we cannot afford to regress."
            ),
        )

    def test_from_object_list_and_link_layers_still_exist(self) -> None:
        module_name, application = _resolve("Application", _APPLICATION_MODULE)
        self.assertIsNotNone(application, _moved(f"Application is not importable from {module_name}."))
        self.assertTrue(
            callable(getattr(application, "from_object_list", None)),
            _moved(
                "Application.from_object_list is gone. v0.1.12 builds the stack programmatically "
                "through it (it is bacpypes3's own from_args path) precisely to avoid from_json's "
                "kebab-case encoding, which was never verified against a live device."
            ),
        )
        try:
            source = inspect.getsource(application)
        except (OSError, TypeError) as exc:  # pragma: no cover - source ships in the wheel
            self.skipTest(f"bacpypes3 Application source unavailable ({exc}); cannot introspect link_layers")
        self.assertIn(
            "link_layers",
            source,
            _moved(
                "Application no longer keeps link_layers. The foreign-device registration wait "
                "reads app.link_layers[...].bbmdRegistrationStatus; without it, a BBMD that "
                "refuses us is silent again — which is the v0.1.12 bug."
            ),
        )


@unittest.skipUnless(_INSTALLED, _SKIP_REASON)
class ForeignDeviceContractTests(unittest.TestCase):
    """The foreign-device registration surface: the reason v0.1.12 exists."""

    def test_network_port_object_exposes_the_foreign_device_properties(self) -> None:
        module_name, network_port_object = _resolve("NetworkPortObject", _NETWORK_PORT_MODULE)
        self.assertIsNotNone(
            network_port_object,
            _moved(f"NetworkPortObject is not importable from {module_name}."),
        )
        names = _declared_names(network_port_object)
        missing = [prop for prop in _FOREIGN_DEVICE_PROPERTIES if prop not in names]
        self.assertEqual(
            missing,
            [],
            _moved(
                f"NetworkPortObject ({module_name}) no longer declares {missing}. Foreign-device "
                "registration in bacpypes3 is DECLARATIVE: from_object_list reads exactly these "
                "properties to build a foreign link layer and register with the BBMD. If they are "
                "renamed, setting them does nothing, bacpypes3 quietly builds a plain broadcast "
                "stack, and cross-subnet devices stay invisible with no error anywhere."
            ),
        )

    def test_ip_mode_still_has_foreign(self) -> None:
        module_name, ip_mode = _resolve("IPMode", _BASETYPES_MODULE)
        self.assertIsNotNone(ip_mode, _moved(f"IPMode is not importable from {module_name}."))
        self.assertTrue(
            hasattr(ip_mode, "foreign"),
            _moved(
                f"IPMode ({module_name}) has no `foreign` member. `bacnetIPMode = IPMode.foreign` "
                "is the single line that turns the stack into a foreign device."
            ),
        )

    def test_host_n_port_accepts_an_explicit_ip_port(self) -> None:
        module_name, host_n_port = _resolve("HostNPort", _BASETYPES_MODULE)
        self.assertIsNotNone(host_n_port, _moved(f"HostNPort is not importable from {module_name}."))
        try:
            default_port = host_n_port("10.0.0.5:47808")
            alternate_port = host_n_port("10.0.0.5:47809")
        except Exception as exc:
            self.fail(
                _moved(
                    f"HostNPort('ip:port') raised {exc!r}. fdBBMDAddress is built from exactly that "
                    "string form (the engine always passes an explicit port rather than relying on "
                    "a default), so a construction failure kills foreign-device mode outright."
                )
            )
        # Checked before dereferencing .address below, so a missing attribute
        # reports this message rather than a bare AttributeError.
        parsed_ports = (("47808", default_port), ("47809", alternate_port))
        self.assertEqual(
            [label for label, parsed in parsed_ports if not hasattr(parsed, "address")],
            [],
            _moved(
                "HostNPort has no `.address`. bacpypes3's own from_object_list does "
                "`link_layer.register(obj.fdBBMDAddress.address, ...)` — registration "
                "cannot happen without it."
            ),
        )
        self.assertIn(
            "10.0.0.5",
            str(default_port.address),
            _moved("HostNPort('10.0.0.5:47808').address does not carry the host it was given."),
        )
        # If the explicit port were being dropped or overridden, both renders
        # would be identical. This holds whichever way bacpypes3 renders the
        # default port (some stacks elide :47808), because 47809 is not default.
        self.assertNotEqual(
            str(default_port.address),
            str(alternate_port.address),
            _moved(
                "HostNPort renders ':47808' and ':47809' identically — the explicit port is being "
                "dropped. A BBMD on a non-default port would then be registered against on the "
                "wrong port, and the run would look like an unreachable BBMD."
            ),
        )

    def test_bip_foreign_still_exposes_the_registration_status_sentinels(self) -> None:
        # bbmdRegistrationStatus is an INSTANCE attribute set in __init__, and
        # BIPForeign cannot be constructed here without a socket and a running
        # loop. So this reads the pinned source. That is the honest ceiling of
        # what introspection can show: it proves the attribute, its sentinels
        # and its transitions still exist in the code we poll — not that they
        # behave as documented on the wire. Only a real BBMD proves that.
        module_name, bip_foreign = _resolve("BIPForeign", _BIP_FOREIGN_MODULE)
        self.assertIsNotNone(bip_foreign, _moved(f"BIPForeign is not importable from {module_name}."))
        try:
            source = inspect.getsource(bip_foreign)
        except (OSError, TypeError) as exc:  # pragma: no cover - source ships in the wheel
            self.skipTest(f"bacpypes3 BIPForeign source unavailable ({exc}); cannot introspect sentinels")

        self.assertIn(
            "bbmdRegistrationStatus",
            source,
            _moved(
                "BIPForeign no longer has bbmdRegistrationStatus. The bounded registration wait "
                "polls exactly this attribute, and it is the ONLY way a BBMD refusal is "
                "distinguishable from an empty network — the bug this release fixes."
            ),
        )
        # -2 = unregistered: the initial value, and what "still -2 after the
        # grace period" means (no response from the BBMD at all).
        self.assertRegex(
            source,
            r"bbmdRegistrationStatus\s*=\s*-2",
            _moved("BIPForeign no longer initialises bbmdRegistrationStatus to -2 (unregistered)."),
        )
        # -1 = in process: the state the wait must keep waiting through rather
        # than mistake for a refusal.
        self.assertRegex(
            source,
            r"bbmdRegistrationStatus\s*(?:==|=|!=)\s*-1",
            _moved("BIPForeign no longer uses -1 (registration in process) for bbmdRegistrationStatus."),
        )
        # > 0 = a BVLL result code: the refusal path, whose code we name in the
        # failure message so the BBMD admin has something to act on.
        self.assertRegex(
            source,
            r"bbmdRegistrationStatus\s*=\s*[^\n]*bvlciResultCode",
            _moved(
                "BIPForeign no longer assigns the BVLL result code into bbmdRegistrationStatus. A "
                "positive status is how a BBMD refusal is reported, and we name that code in the "
                "run's failure message."
            ),
        )
        # 0 = OK, signalled by the registration event bacpypes3 sets only on a
        # zero result code.
        self.assertIn(
            "_registration_event",
            source,
            _moved("BIPForeign no longer signals successful (status 0) registration via _registration_event."),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
