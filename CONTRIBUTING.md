# Contributing

## Ground rules

- **No third-party runtime dependencies.** Standard library only.
- **Tests first.** Version comparison, path safety and transactions need tests
  before implementation.
- **Never weaken security defaults.** `siglevel=required` is the default;
  `TrustAll` requires an explicit opt-in and a warning.
- **Keep the on-disk format stable.** Bump `format` only with a migration path.

## Development

```bash
make dev
make test
make lint
make typecheck
```

Python 3.9 is the minimum. CI runs 3.9 and the latest stable.

## Commit style

Imperative, scoped: `fix(resolver): detect provides cycles`,
`feat(transaction): atomic conffile upgrades`.

## Pull requests

- Describe the user-visible change and the failure mode it fixes.
- Add or update tests.
- Update `docs/` if the package format, repo format, config or CLI changes.
- Do not commit `dist/`, `.tpkg` files or signing keys.

## Adding a package manifest

1. Add `packaging/manifests/<name>.json`.
2. Add any `.install` script under `packaging/scripts/`.
3. Add the package to the `ORDER` array in `packaging/build-packages.sh`
   (dependencies first).
4. Verify with a local `file://` repository.
