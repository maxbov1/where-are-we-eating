# Contributing

## Development setup

Follow the [README quick start](README.md#quick-start). Copy `.env.example` to
`.env`, then configure a personal Google Places key and AWS profile. Never
commit credentials or use root AWS access keys.

## Testing

Run these before opening a pull request:

```bash
python -m compileall -q src
PYTHONPATH=src python -m pytest
git diff --check
```

Tests should not require network access. Provider calls should be mocked at the
adapter boundary. Use live Google Places or OpenTable calls only for deliberate
manual checks with personal credentials.

## Coding standards

- Keep domain models and provider boundaries small and typed.
- Keep preference aggregation and ranking deterministic.
- Preserve source URLs and checked timestamps for external evidence.
- Treat missing availability as unknown, never as available.
- Keep OpenTable booking behind explicit organizer confirmation.
- Do not log API keys, JWT secrets, passwords, cookies, or reservation details.

## Pull Request Process

Describe the user-facing behavior, provider/API changes, and local verification
performed. Update README or architecture documentation when setup, tool scope,
security boundaries, or data flow changes.
