# cru-flags

[![PyPI](https://img.shields.io/pypi/v/cru-flags.svg)](https://pypi.org/project/cru-flags/)
[![license](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)

> **Status: AI-generated, not actively maintained.** This library was
> authored primarily by an AI assistant against the specification in
> [`docs/design.md`](docs/design.md) and is not on anyone's active
> roadmap. Dependabot keeps dependencies and security advisories up to
> date automatically (patch + minor bumps auto-merge; majors require
> manual review), but feature work, bug fixes, and other changes
> happen on a best-effort basis. **Pull requests and issues are
> welcome** — they may take time to be reviewed. See
> [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution workflow.

The official Python client for Cru's pipeline feature-flag service. It reads
one URL from the environment, polls it in the background, and answers flag
lookups from memory:

```python
from cru_flags import flags

if flags.enabled("checkout_v2"):
    ...
```

`enabled()` does no I/O, never blocks, and **never raises** — unknown flags,
a missing `CRU_FLAGS_URL`, and an unreachable flag service all answer `False`.
Zero runtime dependencies, Python 3.11+, fully typed.

---

## Install

```sh
pip install cru-flags
```

Then set the flag document URL for the environment the process runs in — the
pipeline injects this for deployed services:

```sh
export CRU_FLAGS_URL=https://deploys.cru.org/flags/<project>/<environment>
```

`<environment>` is `release-candidate` or `production`.

---

## Quickstart

### The 99% path

```python
from cru_flags import flags

flags.enabled("pilot_banner")  # -> True / False, never raises
```

`flags` is a module-level client built from the environment. Importing it
starts nothing; the background poller starts on your first lookup.

### Waiting for the first fetch at startup

```python
from cru_flags import flags

if not flags.ready(timeout=3.0):
    log.info("cru-flags: still warming up; flags default to off")
```

`ready()` blocks until the first fetch attempt *completes* — success or
failure — and returns whether that happened within `timeout`. It returns
`False` immediately when no `CRU_FLAGS_URL` is configured.

### Inspecting the current document

```python
import json

from cru_flags import flags

json.dumps(flags.snapshot())
# {"Project": "ararat", "Environment": "release-candidate", "Version": 3,
#  "NotifySlack": true, "Flags": {"pilot_banner": {"Enabled": true, ...}}}
```

`snapshot()` returns a plain, JSON-serializable deep copy of the last document
received (`{}` before the first success) — handy on a `/health` endpoint.

### Explicit construction (tests, DI, non-default tuning)

```python
from cru_flags import Client

client = Client(
    url="https://deploys.cru.org/flags/ararat/production",
    poll_seconds=30.0,  # refresh interval, ±20% jitter
    fetch_timeout=2.0,  # per-request socket timeout
    on_error=None,  # None -> warn on the "cru_flags" logger
)

client.enabled("pilot_banner")
client.close()  # stop the poller (optional; the thread is a daemon)
```

`url=None` (the default) reads `CRU_FLAGS_URL` on first use. `on_error` is
called **only on health transitions** — with the exception when polling starts
failing, with `None` when it recovers — so a long outage logs once, not once
per poll.

---

## Public API

| Entry point | Purpose |
| --- | --- |
| `flags` | Module-level `Client()` built from `CRU_FLAGS_URL`. |
| `Client(url=None, poll_seconds=30.0, fetch_timeout=2.0, on_error=None)` | Explicit client for tests, DI, or non-default tuning. |
| `Client.enabled(name)` | `bool` — is this flag on? Never raises, never blocks. |
| `Client.ready(timeout=None)` | `bool` — block until the first fetch attempt completes. |
| `Client.snapshot()` | `dict` — JSON-serializable copy of the last document. |
| `Client.close()` | Stop the background poller. |

---

## Behavioural contract

The library is designed to be **fail-static**: it is allowed to be out of
date, but never allowed to be slow, loud, or fatal. Precisely:

| Situation | Behaviour |
| --- | --- |
| `CRU_FLAGS_URL` unset (or empty, or not http/https) | Inert: every flag `False`, no thread, no socket, no warnings. |
| Before the first successful fetch | Every flag `False`. |
| Flag name unknown, or `Enabled` missing | `False`. |
| `Enabled` is not literally `true` (e.g. `"true"`, `1`, `null`) | `False` — a malformed document reads as off. |
| Steady state | One `GET` per `poll_seconds` ±20% jitter, with `If-None-Match`; `304` keeps the current snapshot. |
| `404` from the service | "No document published yet" — empty snapshot, **not** an error, no warning. |
| `400` / `5xx` / timeout / DNS failure / malformed JSON | Last-known-good snapshot stays in force **indefinitely** (no TTL, no expiry to `False`). One warning on the transition into failure, one on recovery. |
| Retries | None within a poll; the next poll *is* the retry. |
| Process exit | The poller is a daemon thread and never delays interpreter shutdown. |
| Threads | `enabled()` is safe from any thread; snapshot updates are a single atomic swap of an immutable document. |

Every row above is covered by a test. The reasoning behind the surprising
ones — no TTL, `404`-is-data, transition-only logging — is in
[`docs/design.md`](docs/design.md).

---

## Local development

This repo pins the exact Python version in [`.tool-versions`](.tool-versions)
(read by [`asdf`](https://asdf-vm.com/) locally and by CI, so the two cannot
drift) and uses [`uv`](https://docs.astral.sh/uv/) for the virtualenv:

```sh
asdf plugin add python   # one-time, if not already set up
asdf install
uv venv --python "$(awk '/^python /{print $2}' .tool-versions)"
uv pip install -e ".[dev]"
source .venv/bin/activate

ruff check . && ruff format --check .
mypy
pytest
python -m build
```

There is one networked check that CI deliberately does not run:

```sh
python scripts/verify_live.py
```

It fetches the real public document for `ararat/release-candidate` and asserts
that it parses and that a second conditional request returns `304`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the workflow and
[`docs/design.md`](docs/design.md) for the design rationale.

---

## Releasing

Releases are automated. [release-please](https://github.com/googleapis/release-please)
watches Conventional Commits on `main` and maintains a release PR; merging it
tags the version, publishes a GitHub Release, and triggers
`.github/workflows/release.yml`, which builds the sdist + wheel and uploads
them to PyPI via **Trusted Publishing** (OIDC — there is no PyPI token in this
repository).

The very first publish works through PyPI's *pending publisher* mechanism: the
`cru-flags` project does not exist on PyPI yet, so the pending publisher
configured for this repository and the `pypi` environment creates it on the
first successful upload. No manual `twine upload` is needed at any point.

---

## License

[BSD-3-Clause](LICENSE).
