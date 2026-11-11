# Security policy

Dormant archives and deletes folders on your machine. Bugs that can lose data are treated as security bugs.

## Reporting a vulnerability

Please report privately through [GitHub Security Advisories](https://github.com/Om-anand-0/Dormant/security/advisories/new). Do not open a public issue, pull request or discussion for a suspected vulnerability.

Include what you can:

- Dormant version (`dormant --version`), OS, shell and Python version
- Steps to reproduce, ideally a small directory tree or script that triggers it
- What happened and what you expected
- Whether any data was lost

## In scope

- **Data loss**: any way Dormant deletes or corrupts files it shouldn't, or loses a folder it archived (for example a source folder removed before its archive is durable, or an archive that fails to restore).
- **Path escape**: deleting, archiving or writing outside a resolved target folder, including through symlinks, `..` components, junctions or Windows path tricks.
- **Archive extraction issues**: crafted or corrupted archives that write outside the target, bypass checksum verification, or exhaust disk or memory on extraction.
- **Shell hooks**: anything that makes the `cd` hook run unintended commands, for example through crafted directory names or marker files.
- **Unexpected network access**: any network call other than running the user's own package manager in reinstall or purge mode.

Bugs in third-party package managers that Dormant invokes on reinstall are out of scope; report those upstream.

## What to expect

- Acknowledgement within 3 business days.
- An initial assessment within 7 days, telling you whether we accept the report and how we plan to fix it.
- For accepted reports, a fix released as soon as practical, usually within 30 days, followed by a published GitHub Security Advisory crediting you unless you prefer otherwise.

Dormant is maintained by one person in their spare time, so these are goals, not guarantees. Please give us a reasonable chance to fix the issue before disclosing it.

## Supported versions

Before 1.0, only the latest minor release gets security fixes.

| Version | Supported |
| --- | --- |
| Latest 0.x minor | Yes |
| Older 0.x releases | No |
