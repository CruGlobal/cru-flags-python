#!/usr/bin/env python3
"""Opt-in live check against the real, public flag service.

Deliberately **not** part of CI: this talks to production, and CI must not go
red because a service is mid-redeploy. Run it by hand when changing the fetch
path.

    python scripts/verify_live.py [url]

Asserts that a real document parses through the client, and that a second,
conditional request using the ETag the client stored comes back
`304 Not Modified`.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

from cru_flags import Client, __version__

LIVE_URL = "https://deploys.cru.org/flags/ararat/release-candidate"
EXPECTED_KEYS = ("Project", "Environment", "Version", "NotifySlack", "Flags")
NOT_MODIFIED = 304
FETCH_TIMEOUT = 10.0


class LiveCheckError(Exception):
    """A live assertion failed."""


def _fetch_document(client: Client) -> dict[str, Any]:
    """Return the live document, or raise if the client could not get one."""
    if not client.ready(timeout=15.0):
        message = "the first fetch attempt did not complete within 15s"
        raise LiveCheckError(message)

    document = client.snapshot()
    if not document:
        message = "snapshot is empty — the service returned no document"
        raise LiveCheckError(message)

    print("document:")
    print(json.dumps(document, indent=2, sort_keys=True))
    print()

    missing = [key for key in EXPECTED_KEYS if key not in document]
    if missing:
        message = f"document is missing expected keys: {missing}"
        raise LiveCheckError(message)
    print(f"PASS: document has every expected key {list(EXPECTED_KEYS)}")
    return document


def _report_flags(client: Client, document: dict[str, Any]) -> None:
    """Print what the client answers for every flag in the document."""
    flags = document["Flags"]
    if not isinstance(flags, dict):
        message = f"Flags is a {type(flags).__name__}, expected an object"
        raise LiveCheckError(message)
    for name in sorted(flags):
        print(f"      enabled({name!r}) -> {client.enabled(name)}")


def _stored_etag(client: Client) -> str:
    """Return the ETag the client kept from the live response."""
    etag = client._etag  # noqa: SLF001 - verifying the client's own bookkeeping
    if not etag:
        message = "no ETag stored, so conditional requests cannot work"
        raise LiveCheckError(message)
    print(f"PASS: client stored ETag {etag}")
    return etag


def _check_conditional_request(url: str, etag: str) -> None:
    """Assert that revalidating with `etag` returns 304 Not Modified."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "If-None-Match": etag},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        with error:
            status = error.code

    if status != NOT_MODIFIED:
        message = f"conditional request returned {status}, expected {NOT_MODIFIED}"
        raise LiveCheckError(message)
    print(f"PASS: conditional request with If-None-Match returned {NOT_MODIFIED}")


def main(argv: list[str]) -> int:
    """Run the live check; return a process exit status."""
    url = argv[1] if len(argv) > 1 else LIVE_URL
    print(f"cru-flags {__version__} — live verification")
    print(f"url: {url}\n")

    client = Client(url=url, poll_seconds=60.0, fetch_timeout=FETCH_TIMEOUT)
    try:
        document = _fetch_document(client)
        _report_flags(client, document)
        _check_conditional_request(url, _stored_etag(client))
    except LiveCheckError as failure:
        print(f"FAIL: {failure}")
        return 1
    finally:
        client.close()

    print("\nOK: live verification passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
