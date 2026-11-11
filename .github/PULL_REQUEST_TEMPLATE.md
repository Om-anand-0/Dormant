## What and why

<!-- What does this change, and why? Link the issue: Fixes #123 -->

## Checklist

- [ ] Tests added or updated (round-trip test for a new detector)
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` and `uv run pytest -m "not slow"` pass locally
- [ ] Changelog line added under `## [Unreleased]` (or not user-visible)
- [ ] Docs updated (README, `docs/`, `--help` text) where behavior changed
- [ ] Commit messages follow Conventional Commits
- [ ] New runtime dependency? Justified in one line below, or none added

## Cross-platform impact

- [ ] Linux
- [ ] macOS
- [ ] Windows

<!-- Which platforms does this touch, and how was each checked? Note paths, file locks, symlinks, shells. -->

## Safety invariants

- [ ] I've considered the [safety invariants](https://github.com/Om-anand-0/Dormant/blob/main/CLAUDE.md#safety-invariants-never-break-these): nothing is deleted before its archive is durable, only target paths are touched, symlinks are not followed, and a failure leaves the source whole
