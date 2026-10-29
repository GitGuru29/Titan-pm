# `.tpkg` Package Format

A `.tpkg` is a **gzip-compressed tar** so the runtime and the builder need only
the Python standard library.

Filename:

```
{name}-{pkgver}-{pkgrel}-{arch}.tpkg
titan-hwm-3.1.0-1-x86_64.tpkg
```

## Layout

```
METADATA.json
FILES.json
INSTALL                # optional
payload/usr/local/bin/titan-hwm
payload/etc/titan-hwm/config
...
```

The `payload/` prefix is stripped on install; paths are absolute relative to the
target root.

## `METADATA.json`

```json
{
  "format": 1,
  "name": "titan-hwm",
  "version": "3.1.0-1",
  "arch": "x86_64",
  "description": "Titan Hardware Manager",
  "url": "https://…",
  "license": ["Apache-2.0"],
  "depends": ["titan-ui>=1.0.0"],
  "provides": ["titan-hwm"],
  "conflicts": ["titan-hwm-old"],
  "replaces": [],
  "system_depends": ["qt6-base>=6.7"],
  "backup": ["etc/titan-hwm/config"],
  "setuid": [],
  "installed_size": 1234567,
  "build_date": "2026-01-01T00:00:00Z",
  "files_count": 42
}
```

- `version` already includes the release: `pkgver-pkgrel`.
- `depends` / `provides` / `conflicts` use pacman dependency syntax.
- `system_depends` are checked against pacman with `pacman -Q` when available.
- `backup` paths are treated as conffiles (`pacnew` / `pacsave`).
- `setuid` lists the only paths allowed to keep setuid/setgid bits.

## `FILES.json`

```json
[
  {"path": "usr/local/bin/titan-hwm", "type": "file", "mode": "0755", "sha256": "…"},
  {"path": "usr/local/bin/titan-hwm-cli", "type": "symlink", "target": "titan-hwm"}
]
```

Only regular files and symlinks are allowed. Directories are created as needed.
Hardlinks are rejected.

## `INSTALL`

POSIX shell sourced by `/bin/sh`. Supported functions:

```
pre_install post_install pre_upgrade post_upgrade pre_remove post_remove
```

Environment:

```
TITANPKG_PACKAGE=titan-hwm
TITANPKG_VERSION=3.1.0-1
TITANPKG_ROOT=/
TITANPKG_ACTION=upgrade
```

## Determinism

The builder must:

- sort entries by path;
- set `mtime` to `SOURCE_DATE_EPOCH` (or `0`);
- set `uid=gid=0`, `uname=gname=root`;
- omit pax extended headers;
- use gzip with a fixed mtime.

Same inputs must produce the same SHA-256.

## Safety rules

Rejected at install time:

- absolute paths, `..`, empty segments;
- paths outside `allowed_prefixes`;
- symlinks that escape the allowed prefixes;
- hardlinks;
- files that pacman owns;
- setuid bits not declared in `setuid`.
