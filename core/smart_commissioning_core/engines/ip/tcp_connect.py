"""Built-in TCP-connect provider with a dependency-injected transport seam."""

from __future__ import annotations

import asyncio
import errno
import inspect
import socket
from collections.abc import Awaitable, Callable
from typing import Protocol

from smart_commissioning_core.engines.ip.models import (
    ProviderDiagnosticV1,
    TCPPortObservationV1,
    TCPProbeRequestV1,
    TCPProviderCapabilityV1,
)

_PORT_HINTS: dict[int, str] = {
    80: "http",
    443: "https",
    502: "modbus",
    1883: "mqtt",
}

_NETWORK_UNREACHABLE = {
    errno.ENETDOWN,
    errno.ENETUNREACH,
    10050,  # WSAENETDOWN
    10051,  # WSAENETUNREACH
}
_HOST_UNREACHABLE = {
    errno.EHOSTUNREACH,
    getattr(errno, "EHOSTDOWN", -1),
    10064,  # WSAEHOSTDOWN
    10065,  # WSAEHOSTUNREACH
}
_PERMISSION_DENIED = {
    errno.EACCES,
    errno.EPERM,
    10013,  # WSAEACCES
}
_REASONS = {
    "cancelled": "The TCP probe was cancelled.",
    "timed_out": "The TCP connection did not complete before the application deadline.",
    "network_unreachable": "The network path was unreachable.",
    "host_unreachable": "The target host was unreachable.",
    "permission_denied": "The operating system denied the TCP connection.",
    "provider_error": "The TCP provider could not complete the probe.",
}


def provider_capability(
    *,
    protocol: str,
    port: int,
) -> TCPProviderCapabilityV1:
    """Return a stable capability result before any socket is created."""

    if protocol == "tcp":
        return TCPProviderCapabilityV1(
            requested_protocol="tcp",
            supported=True,
            reason_code="supported_protocol",
            reason="The built-in provider supports TCP connect probes.",
        )
    return TCPProviderCapabilityV1(
        requested_protocol="udp",
        supported=False,
        reason_code="unsupported_protocol",
        reason="The built-in TCP provider does not support UDP probes.",
        recommended_provider="bacnet_discovery" if port == 47808 else None,
    )


class TCPWriter(Protocol):
    """Smallest writer surface the provider owns and must close."""

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class TCPConnectTransport(Protocol):
    """External socket boundary injected into the provider."""

    async def connect(
        self,
        target_ip: str,
        port: int,
        *,
        source_ip: str | None,
    ) -> TCPWriter: ...


class AsyncioTCPConnectTransport:
    """Numeric-only asyncio adapter used by the built-in provider."""

    async def connect(
        self,
        target_ip: str,
        port: int,
        *,
        source_ip: str | None,
    ) -> TCPWriter:
        _reader, writer = await asyncio.open_connection(
            target_ip,
            port,
            family=socket.AF_INET,
            flags=socket.AI_NUMERICHOST,
            local_addr=(source_ip, 0) if source_ip is not None else None,
        )
        return writer


CancelCheck = Callable[[], bool | Awaitable[bool]]


def _not_cancelled() -> bool:
    return False


async def probe_tcp_connect(
    request: TCPProbeRequestV1,
    *,
    transport: TCPConnectTransport | None = None,
    is_cancelled: CancelCheck = _not_cancelled,
) -> TCPPortObservationV1:
    """Connect once and close within the request's overall deadline."""

    capability = provider_capability(
        protocol=request.protocol,
        port=request.port,
    )
    if not capability.supported:
        return _observation(
            request,
            outcome="unsupported",
            diagnostic=ProviderDiagnosticV1(
                code="unsupported_protocol",
                reason=capability.reason,
            ),
            capability=capability,
        )

    connected = False
    selected_transport = transport or AsyncioTCPConnectTransport()
    try:
        async with asyncio.timeout(request.application_deadline_s):
            if await _cancel_requested(is_cancelled):
                return _observation(
                    request,
                    outcome="cancelled",
                    diagnostic=ProviderDiagnosticV1(
                        code="cancelled",
                        reason=_REASONS["cancelled"],
                    ),
                )
            if is_cancelled is _not_cancelled:
                writer = await selected_transport.connect(
                    request.target_ip,
                    request.port,
                    source_ip=request.source_ip,
                )
            else:
                writer = await _connect_or_stop(
                    selected_transport,
                    request,
                    is_cancelled=is_cancelled,
                )
            if writer is None:
                return _observation(
                    request,
                    outcome="cancelled",
                    diagnostic=ProviderDiagnosticV1(
                        code="cancelled",
                        reason=_REASONS["cancelled"],
                    ),
                )
            connected = True
            writer.close()
            await writer.wait_closed()
    except asyncio.CancelledError:
        if connected:
            return _observation(
                request,
                outcome="connected",
                diagnostic=ProviderDiagnosticV1(
                    code="connected",
                    reason="TCP connection established.",
                ),
                cleanup_diagnostic=ProviderDiagnosticV1(
                    code="cleanup_cancelled",
                    reason="TCP writer cleanup was cancelled.",
                ),
            )
        return _observation(
            request,
            outcome="cancelled",
            diagnostic=ProviderDiagnosticV1(
                code="cancelled",
                reason=_REASONS["cancelled"],
            ),
        )
    except ConnectionRefusedError as error:
        if connected:
            return _connected_with_cleanup_error(request, error)
        return _observation(
            request,
            outcome="connection_refused",
            diagnostic=ProviderDiagnosticV1(
                code="connection_refused",
                reason="The target refused the TCP connection.",
                errno=_sanitized_errno(error),
            ),
        )
    except TimeoutError as error:
        if connected:
            return _observation(
                request,
                outcome="connected",
                diagnostic=ProviderDiagnosticV1(
                    code="connected",
                    reason="TCP connection established.",
                ),
                cleanup_diagnostic=ProviderDiagnosticV1(
                    code="cleanup_deadline_exceeded",
                    reason="TCP writer cleanup reached the application deadline.",
                    errno=_sanitized_errno(error),
                ),
            )
        error_number = _sanitized_errno(error)
        code = "timed_out" if error_number is not None else "application_deadline_exceeded"
        return _observation(
            request,
            outcome="timed_out",
            diagnostic=ProviderDiagnosticV1(
                code=code,
                reason=_REASONS["timed_out"],
                errno=error_number,
            ),
        )
    except OSError as error:
        if connected:
            return _connected_with_cleanup_error(request, error)
        outcome = _outcome_for_os_error(error)
        return _observation(
            request,
            outcome=outcome,
            diagnostic=ProviderDiagnosticV1(
                code=outcome,
                reason=_REASONS[outcome],
                errno=_sanitized_errno(error),
            ),
        )
    except Exception as error:
        if connected:
            return _connected_with_cleanup_error(request, error)
        return _observation(
            request,
            outcome="provider_error",
            diagnostic=ProviderDiagnosticV1(
                code="provider_error",
                reason=_REASONS["provider_error"],
            ),
        )
    return _observation(
        request,
        outcome="connected",
        diagnostic=ProviderDiagnosticV1(
            code="connected",
            reason="TCP connection established.",
        ),
    )


def _observation(
    request: TCPProbeRequestV1,
    *,
    outcome: str,
    diagnostic: ProviderDiagnosticV1,
    cleanup_diagnostic: ProviderDiagnosticV1 | None = None,
    capability: TCPProviderCapabilityV1 | None = None,
) -> TCPPortObservationV1:
    return TCPPortObservationV1(
        target_ip=request.target_ip,
        port=request.port,
        protocol=request.protocol,
        outcome=outcome,
        diagnostic=diagnostic,
        capability=capability
        or provider_capability(protocol=request.protocol, port=request.port),
        cleanup_diagnostic=cleanup_diagnostic,
        port_hint=_PORT_HINTS.get(request.port),
    )


def _sanitized_errno(error: OSError) -> int | None:
    raw = getattr(error, "winerror", None)
    if raw is None:
        raw = error.errno
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 65535:
        return None
    return raw


def _connected_with_cleanup_error(
    request: TCPProbeRequestV1,
    error: Exception,
) -> TCPPortObservationV1:
    error_number = _sanitized_errno(error) if isinstance(error, OSError) else None
    return _observation(
        request,
        outcome="connected",
        diagnostic=ProviderDiagnosticV1(
            code="connected",
            reason="TCP connection established.",
        ),
        cleanup_diagnostic=ProviderDiagnosticV1(
            code="cleanup_provider_error",
            reason="TCP writer cleanup did not complete normally.",
            errno=error_number,
        ),
    )


def _outcome_for_os_error(error: OSError) -> str:
    error_number = _sanitized_errno(error)
    if error_number in _NETWORK_UNREACHABLE:
        return "network_unreachable"
    if error_number in _HOST_UNREACHABLE:
        return "host_unreachable"
    if error_number in _PERMISSION_DENIED:
        return "permission_denied"
    return "provider_error"


async def _cancel_requested(check: CancelCheck) -> bool:
    value = check()
    if inspect.isawaitable(value):
        value = await value
    return bool(value)


async def _connect_or_stop(
    transport: TCPConnectTransport,
    request: TCPProbeRequestV1,
    *,
    is_cancelled: CancelCheck,
) -> TCPWriter | None:
    """Race the socket boundary against a bounded Stop poll."""

    connect_task = asyncio.create_task(
        transport.connect(
            request.target_ip,
            request.port,
            source_ip=request.source_ip,
        )
    )
    stop_task = asyncio.create_task(_wait_until_cancelled(is_cancelled))
    try:
        done, _pending = await asyncio.wait(
            {connect_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if connect_task in done:
            return await connect_task
        await stop_task
        connect_task.cancel()
        await asyncio.gather(connect_task, return_exceptions=True)
        return None
    finally:
        for task in (connect_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(connect_task, stop_task, return_exceptions=True)


async def _wait_until_cancelled(check: CancelCheck) -> None:
    while not await _cancel_requested(check):
        await asyncio.sleep(0.25)
