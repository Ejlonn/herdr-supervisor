# Release checklist

A beta release remains blocked until all regression, gate, quota, query, Telegram input/output, renderer, backup, migration, restart, and fresh-install tests pass and an independent review accepts the result.

Before any commit or publication:

1. Confirm the product/workspace repositories were not copied into this tree.
2. Scan filenames and contents for bot/API/OAuth tokens, cookies, `.env`, keys, owner/chat/session IDs, hostnames, personal paths, runtime state, logs, locks, outbox/inbox data, and private workflow text.
   Keep the maintainer-specific denylist outside the repository and point `HERDR_RELEASE_PRIVATE_MARKERS` at it when running the suite; the packaged test only knows the packaging user's home path.
3. Inspect all Git objects and full history after commits exist; a clean working tree scan is not a history scan.
4. Run the complete test suite and Python compilation from source, or let CI (`.github/workflows/ci.yml`) run the full gate: Python 3.11 and 3.13 tests with branch coverage ≥ 80%, Ruff, mypy, packaging, entry points, installer dry run, fixture privacy, version consistency, and secret markers.
   Build the sdist and wheel in an isolated environment (`python -m build`), confirm the sdist lists exactly the repository inventory (`MANIFEST.in` is the allowlist), install the wheel into a throwaway prefix, and run every console entry point with `--help`.
5. Run an isolated temporary-HOME install, missing-token doctor, unit validation, source/install hash reconciliation, and default-preserving uninstall.
6. Record every live test and every intentionally deferred action.
7. Obtain separate human authorization for commit, tag, remote creation, push, GitHub publication, or plugin publication.

No repository remote is configured or contacted by the installer. Example configuration contains placeholders only.
