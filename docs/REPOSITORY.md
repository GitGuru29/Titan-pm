# Repository Format

A repository is a static directory served over HTTPS (or `file://`).

```
repo/
├── .nojekyll
├── titanpkg.db.json
├── titanpkg.db.json.sig
├── SHA256SUMS
└── x86_64/
    ├── titan-hwm-3.1.0-1-x86_64.tpkg
    ├── titan-hwm-3.1.0-1-x86_64.tpkg.sig
    └── …
```

## `titanpkg.db.json`

```json
{
  "format": 1,
  "repo": "archtitan",
  "generated": "2026-01-01T00:00:00Z",
  "packages": {
    "titan-hwm": {
      "latest": "3.1.0-1",
      "versions": {
        "3.1.0-1": {
          "file": "x86_64/titan-hwm-3.1.0-1-x86_64.tpkg",
          "sha256": "…",
          "size": 123456,
          "depends": [],
          "provides": ["titan-hwm"],
          "conflicts": [],
          "replaces": [],
          "system_depends": [],
          "backup": ["etc/titan-hwm/config"],
          "setuid": [],
          "description": "Titan Hardware Manager",
          "url": "https://…",
          "license": ["Apache-2.0"]
        }
      }
    }
  }
}
```

Rules:

- `latest` is computed with `vercmp`, never by string sorting.
- Old versions are retained so `titanpkg rollback` works.
- The index is generated only by the builder; never hand-edited.
- The index is signed. Packages may also be signed.

## Hosting

Static hosts only. See `docs/FREE-HOSTING.md`.

### GitHub Pages gotcha

If you use `repo-add`, it creates `titanpkg.db` as a symlink. GitHub Pages does
not serve symlinks. The builder must copy the real file:

```bash
cp -f repo/titanpkg.db.tar.zst repo/titanpkg.db
cp -f repo/titanpkg.files.tar.zst repo/titanpkg.files
```

`titanpkg`'s own format uses plain JSON, so this only matters if you export a
pacman-compatible mirror.

## Mirrors

`repos.d/*.json` may list `servers`. They are tried in order when the primary
`server` fails. All mirrors must serve identical bytes for a given version.
