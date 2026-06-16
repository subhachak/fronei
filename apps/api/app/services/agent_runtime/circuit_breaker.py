from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Callable, TypeVar


logger = logging.getLogger(__name__)
T = TypeVar("T")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    """Raised when a call is blocked by an open circuit."""


class CircuitBreaker:
    def __init__(
        self,
        key: str,
        *,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.key = key
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if (
                self._state == CircuitState.OPEN
                and self._opened_at is not None
                and time.monotonic() - self._opened_at >= self.recovery_timeout
            ):
                self._state = CircuitState.HALF_OPEN
                logger.info("Circuit %r -> HALF_OPEN", self.key)
            return self._state

    def call(self, fn: Callable[[], T]) -> T:
        if self.state == CircuitState.OPEN:
            raise CircuitOpen(f"Circuit {self.key!r} is OPEN")
        try:
            result = fn()
            self._on_success()
            return result
        except CircuitOpen:
            raise
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._opened_at = None
                logger.info("Circuit %r -> CLOSED", self.key)

    def _on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold and self._state != CircuitState.OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning("Circuit %r -> OPEN after %d failures", self.key, self._failure_count)

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None


class CircuitBreakerRegistry:
    _instance: "CircuitBreakerRegistry | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._inner_lock = threading.Lock()

    @classmethod
    def get(cls) -> "CircuitBreakerRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def breaker(self, key: str, **kwargs) -> CircuitBreaker:
        with self._inner_lock:
            if key not in self._breakers:
                self._breakers[key] = CircuitBreaker(key, **kwargs)
            return self._breakers[key]

    def reset(self) -> None:
        with self._inner_lock:
            for breaker in self._breakers.values():
                breaker.reset()
            self._breakers.clear()

    def items(self):
        with self._inner_lock:
            return list(self._breakers.items())
