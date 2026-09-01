# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will use semantic versioning.

## [Unreleased]

### Added

- Clean source-only repository boundary.
- Installable `src/` package and supported module/console entrypoints.
- MIT License and initial public documentation.
- Packaged templates and static assets.
- Privacy and secret scanning safeguards.

### Changed

- Upgraded the database to Schema v10 with transactional legacy ScholarOne URL canonicalization and compact automation run history.
- Automation run summaries are now allowlisted and sanitized at repository write/read boundaries; APIs return structured summaries and the UI renders semantic Chinese text instead of raw JSON.

### Security

- Historical and new automation summaries no longer retain or expose manuscript details, URLs, addresses, raw exceptions, stacks, or unknown fields; failure details use bounded stable codes and owner-scoped journal names.
- Public-release hardening is in progress; this version is not yet a stable production release.
