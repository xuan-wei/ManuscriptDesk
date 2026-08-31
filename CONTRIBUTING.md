# Contributing

Thank you for helping improve Manuscript Desk.

## Before opening a change

1. Use synthetic users, journals, manuscript IDs, email addresses, HTML fixtures, and screenshots.
2. Never copy production state or operational configuration into this repository.
3. Add tests for tenant isolation, authorization, CSRF, error redaction, and failure paths when relevant.
4. Keep provider automation bounded and validate the current browser origin before credentials are entered.

## Local checks

```bash
python -m build
pytest -q
ruff format --check src tests
ruff check src tests
mypy src/manuscript_desk
bandit -q -r src/manuscript_desk
python scripts/check_public_tree.py
```

Do not commit generated distributions, coverage data, local environments, databases, keys, or `.env` files.
