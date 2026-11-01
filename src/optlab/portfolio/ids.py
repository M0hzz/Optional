"""UUIDv7 identifiers (RFC 9562, section 5.7).

Python 3.13's uuid module has no uuid7 (it arrives in 3.14), so this is a small internal
implementation rather than a new dependency:

    48 bits  Unix time in milliseconds
     4 bits  version (0b0111)
    12 bits  rand_a: a counter, seeded randomly each millisecond (RFC 9562 method 1)
     2 bits  variant (0b10)
    62 bits  rand_b: random (os.urandom)

Within one process IDs are strictly increasing: if the clock stands still or steps backwards, the
previous timestamp is reused and the counter advances; if the counter overflows, the timestamp is
advanced by one millisecond. Across processes, uniqueness rests on the 62 random bits (plus the
randomly seeded counter), the same guarantee as uuid4's 122 bits restricted to one millisecond.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0
_MAX_COUNTER = 0xFFF


def uuid7(now_ms: int | None = None) -> uuid.UUID:
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        if ms > _last_ms:
            _last_ms = ms
            _counter = int.from_bytes(os.urandom(2), "big") & 0x7FF   # leave room to count upwards
        else:
            _counter += 1
            if _counter > _MAX_COUNTER:
                _last_ms += 1
                _counter = int.from_bytes(os.urandom(2), "big") & 0x7FF
        ms, counter = _last_ms, _counter
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | counter << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def new_id() -> str:
    """Canonical lowercase string form used as primary key."""
    return str(uuid7())


def timestamp_ms(value: str | uuid.UUID) -> int:
    """The Unix millisecond timestamp embedded in a UUIDv7."""
    u = value if isinstance(value, uuid.UUID) else uuid.UUID(value)
    return u.int >> 80
