# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`cru-flags` — the official Python client for Cru's pipeline feature-flag
service. It polls one public JSON document per (project, environment) in a
background thread and answers `flags.enabled("some_flag")` from an in-memory
snapshot. Read-only; flags are authored in the pipeline, not here.

## docs/design.md is authoritative

Read [`docs/design.md`](docs/design.md) before changing anything. It is the
specification: the wire contract, the behavioural contract, and the rationale
for the decisions that look surprising. Drift between the design doc and the
code is a bug — if a change alters behaviour, update the doc in the same PR.

## Load-bearing design decisions

Settled. Do not revisit these without updating `docs/design.md` first:

- **Zero runtime dependencies.** `urllib.request`, `json`, `threading`,
  `logging` only. A dependency here is inherited by every application at Cru.
- **`enabled()` never raises.** The body is wrapped so a malformed document or
  an interpreter-shutdown race answers `False` instead of propagating.
- **`enabled()` does no I/O in the default background mode** — an attribute and
  two dict keys, no lock, no lazy fetch. Only the opt-in
  `refresh_mode="on-demand"` (§5.1) fetches on the reading thread. Do not
  extend blocking behaviour to background mode.
- **Fail-static, with no TTL.** All flags `False` until the first successful
  fetch; the last-known-good document then persists through failures
  *indefinitely*. Do not add expiry — it would turn a flag-service outage
  into a synchronised fleet-wide behaviour change.
- **`fetch_timeout` is a wall-clock deadline for the whole tick,** not a
  per-socket-operation timeout. Handed straight to `urlopen` it becomes the
  latter, and both a redirect chain and a slow-drip body then run for
  multiples of it. Redirects are followed by `_fetch` under that one
  deadline, to a limit of 3 hops, each `Location` re-validated before it is
  opened; the body is read in `read1` chunks under a 1 MiB cap. Do not
  reintroduce the default opener — it brings `HTTPRedirectHandler`, which
  re-arms the budget on every one of its ten hops, and file/ftp/data handlers
  a redirect could reach. See `docs/design.md` §7, which also records the one
  known residual (DNS is not bounded).
- **`404` is data, not an error.** It means "no document published yet" and
  yields an empty snapshot with no `on_error` call. `400` *is* an error (the
  URL names an environment that does not exist).
- **`Enabled` is checked with `is True`,** not truthiness. A malformed value
  reads as off.
- **Daemon thread, started lazily** on the first `enabled()`/`ready()` call —
  never at import. Import must not start threads or read the environment
  (`CRU_FLAGS_URL`, `CRU_FLAGS_REFRESH_MODE`).
- **Misconfiguration from the *environment* warns; from *code* it raises.** A
  bad `CRU_FLAGS_REFRESH_MODE` falls back to background with one `on_error`
  call; a bad `refresh_mode=` argument raises `ValueError`.
- **±20% jitter** on every sleep, so co-deployed pods de-phase instead of
  stampeding the flag service. On-demand mode has no jitter: real traffic
  already de-phases it.
- **On-demand staleness is anchored on the last *attempt*,** not the last
  success, so a dead flag service costs one request per interval rather than
  one per read. Concurrent readers coalesce onto one fetch under
  `_refresh_lock`.
- **Snapshot is frozen** (`MappingProxyType` / tuples) and published by a
  single atomic attribute swap under a writer-only lock. Readers never lock.
  `snapshot()` returns a thawed deep copy.
- **`on_error` fires on health *transitions* only** (`ok -> failing` with the
  exception, `failing -> ok` with `None`), never per poll.
- **Unknown document keys are ignored.** The service adds keys additively;
  never validate beyond "is a JSON object".

## Toolchain

- **Python 3.13.x** is the development interpreter, pinned in
  [`.tool-versions`](.tool-versions) (asdf reads it locally; CI extracts the
  version from the same file, so local and CI cannot drift). The *shipped*
  library supports 3.11+, verified by a CI matrix.
- [`uv`](https://docs.astral.sh/uv/) manages the virtualenv.
- `ruff` (lint + format), `mypy --strict`, `pytest`, `hatchling` build.

```sh
asdf install                      # once, per .tool-versions
uv venv --python "$(awk '/^python /{print $2}' .tool-versions)"
uv pip install -e ".[dev]"
source .venv/bin/activate
```

## Commands

```sh
ruff check .            # lint
ruff format --check .   # format check (drop --check to reformat)
mypy                    # strict typecheck, config in pyproject.toml
pytest                  # tests
python -m build         # sdist + wheel into dist/

python scripts/verify_live.py   # opt-in, hits the real service (not in CI)
```

Run all five before opening a PR — CI runs exactly these.

## Conventions

- **Failing test first** for bug fixes and features (see
  [`CONTRIBUTING.md`](CONTRIBUTING.md)). Every line of the behavioural
  contract in `docs/design.md` §3–§7 has a corresponding test.
- Prefer the real local HTTP server fixture in `tests/conftest.py` over
  mocking `urlopen`. `urllib` raises `HTTPError` for `304`, and that is the
  sort of detail a mock gets wrong.
- Conventional Commits; release-please cuts releases from `main`. Squash
  merges only.
