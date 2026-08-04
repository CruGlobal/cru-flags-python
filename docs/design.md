# cru-flags — Design Document

## 1. Purpose & Scope

`cru-flags` is the official Python client for Cru's pipeline feature-flag
service. It answers exactly one question, as cheaply and as safely as
possible:

```python
from cru_flags import flags

if flags.enabled("checkout_v2"):
    ...
```

The flag service publishes one JSON document per (project, environment) at a
public HTTP endpoint. This library polls that document in the background and
answers `enabled()` from an in-memory snapshot. It is a **read-only client**:
flags are authored elsewhere (the pipeline UI / API), never by this library.

### In scope for v0.x

- Read `CRU_FLAGS_URL` from the environment; poll that URL in a background
  thread; answer `enabled(name)` from the last document received.
- Conditional requests (`If-None-Match` / `ETag`) so steady-state polling
  costs one 304 per interval.
- An opt-in `refresh_mode="on-demand"` that drops the thread and refreshes
  synchronously on the reading thread, for runtimes that freeze between
  requests (§5.1).
- **Fail-static** semantics: a flag lookup never raises, never blocks, and
  never changes answer because the network broke.
- Zero runtime dependencies (`urllib.request`, `json`, `threading` from the
  standard library), Python >= 3.11, fully typed with a `py.typed` marker.

### Out of scope

- **Writing** flags, or any authentication. The flag documents are public,
  read-only URLs.
- Per-user / percentage targeting, variants, or non-boolean flag values. The
  service models a flag as a boolean with metadata; so does this client.
- Local flag overrides, file-based sources, streaming (SSE/WebSocket), or a
  daemon/sidecar. Polling a small document is sufficient at our scale.
- `asyncio`-native API. The client is thread-based and non-blocking to read,
  which makes it safe to call from async code without an executor.

### Non-goals

- We do **not** try to be a general feature-flag SDK (LaunchDarkly-style).
  There is one document shape, defined by our own service, and this client is
  allowed to know it.
- We do **not** raise on misconfiguration. An application that is missing
  `CRU_FLAGS_URL` must still boot and serve traffic, with every flag off.

---

## 2. Wire contract

The live document (this is a real response from
`https://deploys.cru.org/flags/ararat/release-candidate`):

```json
{
  "Project": "ararat",
  "Environment": "release-candidate",
  "Version": 3,
  "NotifySlack": true,
  "Flags": {
    "pilot_banner": {
      "Enabled": true,
      "Description": "Pilot: flag-gated banner proving the flag service end-to-end (ararat#198)",
      "CreatedAt": "2026-07-31T14:09:01.119Z",
      "UpdatedAt": "2026-07-31T14:09:08.777Z",
      "UpdatedBy": "Omicron7"
    }
  }
}
```

Load-bearing properties:

| Property | Contract |
| --- | --- |
| Top level | JSON object with `Project`, `Environment`, `Version`, `NotifySlack`, `Flags`. PascalCase keys. |
| `Flags` | Object keyed by flag name. Each value is an object with at least `Enabled: bool`; other keys are metadata (`Description`, `CreatedAt`, `UpdatedAt`, `UpdatedBy`). |
| Unknown keys | **Additive.** New top-level keys and new per-flag metadata keys may appear at any time; the client must ignore what it does not understand and must never validate the document beyond "is a JSON object". |
| `ETag` | Present on `200`, currently the stringified `Version` (e.g. `"3"`). Opaque to the client — store and echo it, never parse it. |
| `304 Not Modified` | Returned when `If-None-Match` matches. Body is empty; the previous snapshot stays in force. |
| `404 Not Found` | `{"message": "<project> has no feature flags in <env> yet."}` — a *valid* answer meaning "no document exists yet", not a failure. |
| `400 Bad Request` | `{"message": "\"staging\" has no feature flags. …"}` — the URL names an environment that does not exist. This *is* a failure: the caller's configuration is wrong and someone should see it. |

`enabled(name)` is defined precisely as:

```python
Flags.get(name, {}).get("Enabled") is True
```

The `is True` identity check (not truthiness) is deliberate: a flag whose
`Enabled` is the *string* `"true"`, or `1`, or `null` is a malformed document,
and the safe reading of a malformed document is "off".

---

## 3. Public API

```python
from cru_flags import Client, flags

flags.enabled("checkout_v2")  # -> bool, never raises
flags.ready(timeout=3.0)  # -> bool, never raises
flags.snapshot()  # -> dict, plain JSON-serializable copy
flags.refresh()  # -> bool, refresh now on this thread
flags.close()  # -> None, stop refreshing (mostly for tests)

Client(
    url=None,  # None -> read CRU_FLAGS_URL on first use
    poll_seconds=30.0,  # refresh interval, +/-20% jitter
    fetch_timeout=2.0,  # per-request socket timeout
    on_error=None,  # None -> log to logging.getLogger("cru_flags")
    refresh_mode=None,  # None -> read CRU_FLAGS_REFRESH_MODE, else "background"
)
```

### 3.1 `flags`

`flags` is a module-level `Client()` — the 99% path. It is constructed at
import time but **does nothing at import time**: no environment read, no
socket, no thread. See §5.

### 3.2 `Client.enabled(name) -> bool`

Non-blocking read of the current snapshot (in on-demand mode it may refresh
first; see §5.1). **Never raises** — the entire body
is wrapped so that no bug in this library, no malformed document, and no
interpreter-shutdown race can take down a caller's request path. Anything
unexpected answers `False`.

Unknown flag, unset flag, no document yet, no `CRU_FLAGS_URL`, service down
before the first success — all `False`.

### 3.3 `Client.ready(timeout=None) -> bool`

Blocks until the **first fetch attempt completes** — success *or* failure —
and returns whether it completed within `timeout`. Intended for startup code
that wants "flags are as fresh as they are going to get" before serving:

```python
if not flags.ready(timeout=3.0):
    log.info("cru-flags: still warming up; flags default to off")
```

`ready()` returns `True` after a failed first attempt, because the attempt
did complete and the answer ("all off") is final until the next poll. It
returns `False` immediately, without blocking, when the client is inert
(no URL configured) — there is no attempt to wait for and there never will
be. `timeout=None` waits for the first attempt to finish, which is bounded in
practice by `fetch_timeout`. Never raises.

In on-demand mode there is no background attempt to wait for, so `ready()`
*performs* the first attempt like any other read and then reports that one has
completed; `timeout` is unused.

### 3.4 `Client.snapshot() -> dict[str, Any]`

A plain, deep-copied, JSON-serializable `dict` of the last document received
(`{}` before the first success). Useful for health/debug endpoints:
`json.dumps(flags.snapshot())` round-trips to exactly what the service sent.
The copy exists so a caller cannot mutate library state; the internally
stored snapshot is genuinely immutable (§6).

### 3.5 `Client.refresh(*, force=False) -> bool`

Fetches on the *calling* thread and returns whether the snapshot is fresh: an
attempt has completed and the most recent one succeeded. Without `force` it is
a no-op while the last attempt is younger than `poll_seconds`, so it is cheap
to call per request. Blocks for at most `fetch_timeout`. Never raises.

This is the refresh in on-demand mode (§5.1), and an out-of-band poke in
background mode — e.g. a debug endpoint that wants the current document
without waiting up to `poll_seconds` for it.

### 3.6 `Client.close()`

Stops refreshing, in either mode. Optional — the thread is a daemon and never
delays shutdown — and terminal: a closed client never refreshes again, and closing
*before* the first lookup leaves the client permanently inert (rather than
starting a poller that would exit before its first attempt and strand
`ready()` on an event nobody will set). The last snapshot stays readable.
Mostly useful in tests.

### 3.7 `on_error`

```python
OnError = Callable[[BaseException | None], None]
```

Called **only on health transitions**:

- `ok -> failing`: called with the exception that broke the poll.
- `failing -> ok`: called with `None` (recovery).

Never called per-poll while a failure persists, and never called for `304`
(nothing changed) or `404` (no document yet — a valid state, not an error).
The default implementation logs both transitions at `WARNING` on the
`cru_flags` logger. Exceptions raised *by* `on_error` are swallowed: a broken
error handler must not kill the poller.

---

## 4. Why fail-static

A feature-flag client sits on the hottest path in the application — often
inside a request handler — and it is not the application's job to survive its
telemetry. So the design constraint is inverted relative to a normal HTTP
client: **the flag client is allowed to be wrong, but never allowed to be
loud, slow, or fatal.**

Concretely:

1. **`enabled()` performs no I/O** in the default background mode. It reads
   one attribute and two dict keys: no lazy fetch, no lock acquisition on the
   read path, and therefore no way for a slow network to become a slow
   request. On-demand mode (§5.1) gives this up deliberately, and only for
   deployments that ask for it.
2. **All flags are `False` until the first successful fetch.** A flag guards
   *new* behaviour; the safe answer while we are ignorant is the old
   behaviour. This also makes the "service unreachable at boot" case
   identical to the "flag not created yet" case — one behaviour to reason
   about instead of two.
3. **Last-known-good persists indefinitely.** There is no TTL and no
   expiry-to-`False`. If the flag service is down for six hours, the
   application keeps running the configuration it last saw, which is by
   definition a configuration that was deliberately published. Expiring to
   `False` would convert a flag-service outage into a synchronised,
   fleet-wide behaviour change — exactly the incident we are trying not to
   cause. The freshness question is answered by monitoring (the warning log,
   `ready()`, `snapshot()["Version"]`), not by silently flipping behaviour.
4. **404 is data, not an error.** "This project has no flags yet" is the
   normal state of every project on day one. Treating it as an error would
   mean every new service logs warnings until someone creates a flag.
5. **Nothing raises.** `enabled()` and `ready()` swallow `Exception`
   (not `BaseException` — `KeyboardInterrupt` and `SystemExit` still
   propagate).

---

## 5. Threading model

- **One daemon thread per active client**, named `cru-flags-poller`.
- **Started lazily** on the first `enabled()` / `ready()` call, never at
  import. Importing a library must not start threads: it breaks `--help`,
  breaks `fork()`-based servers that import before forking, and makes
  `import cru_flags` in a test suite a side-effecting act. Lazy start also
  means the environment can be mutated (tests, `dotenv`, a CLI that sets
  `CRU_FLAGS_URL` from an argument) any time before the first lookup.
- **`daemon=True`, unconditionally.** A background refresher must never be
  the reason a process fails to exit. The interpreter is free to shut down
  mid-poll; the thread holds no resource whose loss matters, and `enabled()`
  swallows any teardown-race error.
- **Sleeping is interruptible**: the loop waits on a `threading.Event`, so
  `close()` returns promptly instead of blocking for a poll interval.
- **Jitter**: each sleep is `poll_seconds * uniform(0.8, 1.2)`. Every pod in
  a deployment starts within a few seconds of every other pod; without jitter
  they would synchronise into a thundering herd against the flag service and
  stay synchronised. ±20% de-phases them within a couple of intervals.
- **Inert clients start nothing.** No `CRU_FLAGS_URL` and no explicit `url`
  means no thread, no socket, no warning — just `False`. Local development
  and unit tests get zero overhead and zero noise by default.

All of the above describes `refresh_mode="background"`, the default.

### 5.1 On-demand refresh (`refresh_mode="on-demand"`)

With `refresh_mode="on-demand"` there is **no poller thread at all**. The
refresh happens on whichever thread reads a flag, and only when the snapshot
has aged out:

- Every read (`enabled()`, `ready()`, `snapshot()`) — and every explicit
  `refresh()` — fetches first if the last *attempt* is `poll_seconds` or older,
  and otherwise answers from cache. So a read costs at most one HTTP request
  per `poll_seconds` per process, bounded by `fetch_timeout`.
- Concurrent readers **coalesce**: they queue on one lock, the first fetches,
  and the rest return as soon as that fetch settles rather than issuing their
  own. A burst of N concurrent requests is one request to the flag service.
- Staleness is anchored on the last **attempt**, not the last success. A dead
  flag service therefore costs one failed request per interval, not one per
  read, and the failure is fail-static exactly as in §4.
- No jitter. Jitter exists to de-phase a fleet's timers; on-demand refreshes
  are already de-phased by the arrival of real traffic.
- `close()` is still terminal, and still leaves the last snapshot readable.

Why it exists: on CPU-throttled or scale-to-zero runtimes — Cloud Run, Lambda
outside an invocation — a background poller either does not run between
requests or wakes an idle instance for work nobody asked for. Refreshing on
the request thread is cheap (a conditional GET, usually a `304`, once per
interval) and happens exactly when someone wants an answer.

Selecting it: `Client(refresh_mode="on-demand")`, or
`CRU_FLAGS_REFRESH_MODE=on-demand` in the environment — the variable exists so
the module-level `flags` singleton, which nobody constructs, can be switched by
a deployment rather than a code change. The constructor argument wins over the
environment. An unrecognised *environment* value warns through `on_error` and
falls back to background, because misconfiguration must never stop an app
booting (§1); an unrecognised constructor argument raises, because that is a
typo in code.

The cost is the read-path guarantee: in this mode `enabled()` can block for up
to `fetch_timeout`, once per interval. Everything else — fail-static,
transition-only reporting, no TTL, never raising — is unchanged.

### Environment resolution

`CRU_FLAGS_URL` and `CRU_FLAGS_REFRESH_MODE` are read once, at first use, and
cached — never at import, so tests and `dotenv` can still set them. An empty or
whitespace-only value counts as unset. Only `http`/`https` URLs are accepted;
anything else makes the client inert (with one warning through `on_error`),
because `urllib` would otherwise happily open `file:///etc/passwd`.

---

## 6. Snapshot storage & atomicity

The parsed document is **frozen** before it is published to readers:
`dict` -> `types.MappingProxyType`, `list` -> `tuple`, recursively. The frozen
tree is then assigned to a single attribute — one atomic reference swap —
while holding a lock that only *writers* contend for. Readers never take the
lock.

Why frozen rather than "a dict we promise not to mutate": the snapshot is
shared across every thread in the process, and a single accidental
`snapshot["Flags"]["x"]["Enabled"] = True` in caller code would be an
un-debuggable, cross-thread, non-reproducible bug. `MappingProxyType` makes
that a `TypeError` at the point of the mistake. `snapshot()` hands back a
thawed deep copy so the public API stays ordinary-Python (and
`json.dumps`-able).

Consistency guarantee: a reader sees either the whole old document or the
whole new one, never a half-applied update, because publication is a single
attribute store of an already-built immutable tree.

---

## 7. Fetch algorithm

One tick:

1. Build a `urllib.request.Request` with `Accept: application/json`, a
   `User-Agent` of `cru-flags-python/<version>`, and `If-None-Match: <etag>`
   if an ETag is stored.
2. `urlopen(req, timeout=fetch_timeout)`. **No retries within a tick** — the
   next tick is the retry, and it arrives in `poll_seconds`. Retrying inside
   a tick only multiplies load on a service that is already unhealthy, and
   adds nothing: nobody is waiting on the answer.
3. Outcomes:

   | Outcome | Action | Health |
   | --- | --- | --- |
   | `200` + JSON object | Freeze and publish; store `ETag` | ok |
   | `304` | Keep snapshot and ETag | ok |
   | `404` | Publish empty snapshot; clear ETag | ok |
   | Other status (`400`, `5xx`, …) | Keep snapshot | failing |
   | Timeout / DNS / connection error | Keep snapshot | failing |
   | Body is not JSON, or not a JSON object | Keep snapshot | failing |

4. Report the health transition (§3.7), if any.

The response body is read inside a `with` block so sockets are closed
deterministically; the client uses no connection pooling and no keep-alive,
which is the right trade for one request per 30 seconds.

---

## 8. Testing strategy

Every line of the behavioural contract above is a test. Two harnesses:

1. **A real local HTTP server** (`http.server.ThreadingHTTPServer` on
   `127.0.0.1`, programmable per-request responses, records inbound headers).
   Used for anything where `urllib`'s real behaviour matters: ETag echo, the
   304 path, 404, 500, slow responses, malformed bodies. Mocking `urlopen`
   would test our mock's idea of HTTP; `304` in particular arrives as a
   raised `HTTPError`, which is exactly the kind of detail a mock gets wrong.
2. **Monkeypatched `urlopen`** for the few assertions about *how* we call it:
   the `fetch_timeout` value reaching the socket, and exactly one call per
   tick (no in-tick retries).

Plus: a subprocess test asserting `import cru_flags` starts no threads even
with `CRU_FLAGS_URL` set; a jitter-distribution test; a concurrency test
hammering `enabled()` from many threads across snapshot swaps; and a
`json.dumps`/`loads` round-trip test proving `snapshot()` reproduces the
service's document byte-for-value.

`scripts/verify_live.py` is an **opt-in, networked** check against the real
public endpoint. It asserts a real document parses and that a second
conditional fetch returns `304`. It is deliberately not part of CI: CI must
not fail because a production service is redeploying.

---

## 9. Packaging & distribution

- PyPI distribution `cru-flags`, import package `cru_flags`, `src/` layout,
  `hatchling` backend, static version in `pyproject.toml` (bumped by
  release-please), `py.typed` shipped.
- `requires-python = ">=3.11"`. The library uses only `X | None` unions and
  stdlib available since 3.9; 3.11 is the floor because it is the oldest
  version still receiving security fixes for the lifetime of this package.
- Zero runtime dependencies, enforced by the absence of
  `[project.dependencies]` — a dependency here would be inherited by every
  application at Cru.
- Dev dependencies live in `[project.optional-dependencies].dev` rather than
  PEP 735 `[dependency-groups]` so that Dependabot's `pip` ecosystem sees
  them.
- Publishing uses **PyPI Trusted Publishing** (OIDC) from
  `.github/workflows/release.yml`; there is no API token in the repository.
