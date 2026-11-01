"""HTTP GET with a timeout, a size cap and an identifying User-Agent. Errors never include headers or keys."""
from __future__ import annotations

from typing import Callable

import requests

MAX_BYTES = 8_000_000
Get = Callable[[str, dict], bytes]


class FetchError(Exception):
    pass


def http_get(url: str, headers: dict, timeout: float = 20.0, max_bytes: int = MAX_BYTES) -> bytes:
    try:
        with requests.get(url, headers=headers, timeout=timeout, stream=True) as r:
            if r.status_code != 200:
                raise FetchError(f"HTTP {r.status_code} from {url.split('?')[0]}")
            out = bytearray()
            for chunk in r.iter_content(64 * 1024):
                out += chunk
                if len(out) > max_bytes:
                    raise FetchError(f"response from {url.split('?')[0]} is larger than {max_bytes // 1_000_000} MB")
            return bytes(out)
    except requests.RequestException as exc:
        raise FetchError(f"{type(exc).__name__} fetching {url.split('?')[0]}") from None
