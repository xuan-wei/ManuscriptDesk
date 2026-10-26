# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will use semantic versioning.

## [Unreleased]

### Added

- Independent tenant submission-journey management with all-member search, add/edit/dissolve workflows and card deep links; replaces the manuscript-page merge modal.
- Journey members automatically follow first-submission date (oldest to newest), with explicitly labeled unknown dates first and deterministic identifier ties; the latest known submission supplies representative content, without manual reorder controls.
- Clean source-only repository boundary.
- Installable `src/` package and supported module/console entrypoints.
- MIT License and initial public documentation.
- Packaged templates and static assets.
- Privacy and secret scanning safeguards.
- Per-journal manual updates from manuscript cards and individual submission-journey members.
- Latest Run and Run Log now show run source, scope, start/end time, duration, and a fixed-category Chinese reason for each failed journal.

### Fixed

- Manuscript cards now show the initial submission date under “首次提交” instead of the latest revision's submission date for papers with multiple rounds.

### Changed

- The ongoing manuscript view now includes cards with unacknowledged NEW statuses, including rejection/acceptance decisions, until acknowledged; hidden items and automation working scopes retain their existing behavior.

- Retry explicitly recoverable journal tracking failures once with a fresh provider/browser session; authentication, credential, unsafe-navigation, and unclassified failures remain terminal.
- Editorial Manager failures are now classified into stable, non-sensitive categories (availability, login rejection, page-structure change, data-shape change) at the provider boundary instead of collapsing to a single generic code.
- Reorganized the workflow page into accessible Run Log, Latest Run, and Automation Settings tabs without duplicating the latest entry.
- Upgraded the database to Schema v10 with transactional legacy ScholarOne URL canonicalization and compact automation run history.
- Automation run summaries are now allowlisted and sanitized at repository write/read boundaries; APIs return structured summaries and the UI renders semantic Chinese text instead of raw JSON.

### Security

- Historical and new automation summaries no longer retain or expose manuscript details, URLs, addresses, raw exceptions, stacks, or unknown fields; failure details use bounded stable codes and owner-scoped journal names.
- Per-journal failure diagnostics are drawn from a fixed allowlist; provider markup, selector text, missing header names, folder labels, and Selenium exception types never leave the tracker.
- Public-release hardening is in progress; this version is not yet a stable production release.
