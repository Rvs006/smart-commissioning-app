"""propagate_uvicorn_loggers re-points uvicorn's loggers at the root handlers.

Pure test (no app, no database): uvicorn installs its own StreamHandlers with
propagate=False, so uvicorn.error's unhandled-500 tracebacks never reach the
root rotating file handler and are lost from app.log. The re-pointing clears
those handlers and turns propagation on so every uvicorn record lands in the
file handler.
"""

import logging
import unittest

from app.core.logging import _UVICORN_LOGGER_NAMES, propagate_uvicorn_loggers


class PropagateUvicornLoggersTests(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot each uvicorn logger's handlers/propagate and restore them, so
        # touching the process-global logging tree here cannot leak into any other
        # test in the run.
        saved = {
            name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
            for name in _UVICORN_LOGGER_NAMES
        }

        def _restore() -> None:
            for name, (handlers, propagate) in saved.items():
                uvicorn_logger = logging.getLogger(name)
                uvicorn_logger.handlers = list(handlers)
                uvicorn_logger.propagate = propagate

        self.addCleanup(_restore)

    def test_clears_handlers_and_enables_propagation(self) -> None:
        # Mimic uvicorn's own configuration: an installed handler, propagation off.
        for name in _UVICORN_LOGGER_NAMES:
            uvicorn_logger = logging.getLogger(name)
            uvicorn_logger.handlers = [logging.NullHandler()]
            uvicorn_logger.propagate = False

        propagate_uvicorn_loggers()

        for name in _UVICORN_LOGGER_NAMES:
            uvicorn_logger = logging.getLogger(name)
            self.assertEqual(uvicorn_logger.handlers, [], f"{name} handlers must be cleared")
            self.assertTrue(uvicorn_logger.propagate, f"{name} must propagate to root")

    def test_covers_the_error_logger(self) -> None:
        # uvicorn.error is the one that carries unhandled-500 tracebacks.
        self.assertIn("uvicorn.error", _UVICORN_LOGGER_NAMES)

    def test_is_idempotent(self) -> None:
        propagate_uvicorn_loggers()
        propagate_uvicorn_loggers()
        for name in _UVICORN_LOGGER_NAMES:
            uvicorn_logger = logging.getLogger(name)
            self.assertEqual(uvicorn_logger.handlers, [])
            self.assertTrue(uvicorn_logger.propagate)


if __name__ == "__main__":
    unittest.main()
