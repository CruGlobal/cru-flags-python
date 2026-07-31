# Contributing to cru-flags

Thanks for your interest in contributing. This document covers the
conventions and toolchain expectations for the repository.

## Toolchain

This project pins the exact Python version in a repo-root `.tool-versions`
file. Contributors use [`asdf`](https://asdf-vm.com/) to read that file
locally; CI extracts the version from the same file and hands it to
[`uv`](https://docs.astral.sh/uv/), so local and CI resolve the same
interpreter from the same single source. Install `asdf`, add the Python
plugin, and run `asdf install` from the repo root before creating the
virtualenv.

```sh
asdf plugin add python
asdf install
uv venv --python "$(awk '/^python /{print $2}' .tool-versions)"
uv pip install -e ".[dev]"
source .venv/bin/activate
```

Do **not** bypass `asdf` by installing Python through a different mechanism
(e.g. Homebrew, pyenv, the system package manager) for contribution work —
divergent versions cause hard-to-debug failures and make PR reviews
inconsistent. CI reads the same `.tool-versions` file, so a green local build
implies a green CI build.

There is no committed lockfile. The library has zero runtime dependencies and
its dev dependencies float within the bounds declared in
`[project.optional-dependencies].dev`; Dependabot watches `pyproject.toml` and
raises the floors when a new version lands. `uv.lock` is gitignored.

## Workflow

1. Keep PRs focused on one change. `docs/design.md` is the specification —
   read it first; it explains the behaviour you are about to change and why
   it is the way it is.
2. Design-level changes require updating
   [`docs/design.md`](docs/design.md) in the same PR. The design doc is
   authoritative; drift between docs and code is a bug.
3. Before opening a PR, run the full local check:
   ```sh
   ruff check .
   ruff format --check .
   mypy
   pytest
   python -m build
   ```
   CI runs exactly these five in a single job.
4. Use [Conventional Commits](https://www.conventionalcommits.org/) for
   commit messages and PR titles — `feat:`, `fix:`, `docs:`, `build:`,
   `ci:`, `test:`, `refactor:`, `chore:`. release-please turns them into the
   changelog and the version bump, so the title of your PR is what users
   read in the release notes.
5. PRs merge by **squash** only. The PR title becomes the commit on `main`,
   which is why it has to be a valid Conventional Commit.

## Failing-test-first for bug fixes and features

**Bug fixes and feature PRs must lead with a failing test.** Structure
your commits so the PR history reads:

1. **First commit** — adds a test that reproduces the bug or asserts
   the new feature's behaviour. CI should fail on this commit alone.
2. **Subsequent commits** — the fix or implementation, with the test
   flipping from red to green.

This applies equally to **issue reports**: when filing a bug, include a
minimal failing test (or the flag document + the answer you expected from
`enabled()`) that demonstrates the problem. Reproducible issues are triaged
first.

Why: it proves the behaviour wasn't already covered, documents the
expected outcome, and prevents regressions. On merge the repo uses
squash, so the two commits collapse into one on `main` — but the
reviewing history stays clean.

### Exemptions

The failing-test-first requirement does **not** apply to:

- **Documentation-only changes** (README, docs/**, docstring-only edits).
- **CI / workflow changes** (`.github/**`).
- **Dependency updates** (Dependabot PRs, manual bumps).
- **Chore / refactor** PRs that don't change observable behaviour.

If you're unsure which bucket a PR falls into, include a test — it's
always accepted, even when not required.

## Code conventions

- `mypy --strict` clean, `ruff` clean, `ruff format`ed. No `# type: ignore`
  without a code and a reason.
- **Zero runtime dependencies** in the published library — standard library
  only. Dev dependencies are fine.
- The published library supports Python 3.11+; CI runs the test suite on
  every supported minor version. Do not use syntax or stdlib APIs newer than
  the floor in `requires-python`.
- `enabled()` must remain non-blocking, I/O-free, and non-raising. Anything
  that could raise belongs inside the poller thread, not the read path.
- Prefer the real local HTTP server fixture in `tests/conftest.py` over
  monkeypatching `urlopen`: `urllib` surfaces `304` as a raised `HTTPError`,
  and that is exactly the kind of behaviour a mock gets wrong.
- Tests must not reach the public internet. The only networked check is
  `scripts/verify_live.py`, which is opt-in and excluded from CI.

## License

By contributing, you agree that your contributions will be licensed under
the [BSD-3-Clause License](LICENSE).
