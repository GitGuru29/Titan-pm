# Architecture

## Boundaries

```
                    ┌─────────────────────────────┐
                    │  pacman                      │
                    │  /var/lib/pacman             │
                    │  owns /usr, /etc, …          │
                    └──────────────┬──────────────┘
                                   │ never modified
                    ┌──────────────▼──────────────┐
                    │  titanpkg                    │
                    │  /var/lib/titanpkg           │
                    │  owns only recorded paths    │
                    └──────────────┬──────────────┘
                                   │
              /usr/local, /usr/lib/qt6/qml/ArchTitan,
              /etc/archtitan, /etc/skel, /usr/share/archtitan
```

`titanpkg` is intentionally **not** a pacman replacement. It does not read or
write pacman's database, does not resolve Arch system dependencies, and refuses
to touch a file owned by a pacman package.

## Modules

| Module | Responsibility | Phase |
|---|---|---|
| `vercmp.py` | libalpm-compatible version ordering | 1 |
| `core/models.py` | `Package`, `FileEntry`, `Dependency` | 1 |
| `config.py` | root-aware paths, config/repo loading | 1 |
| `util.py` | atomic writes, hashing, safe paths, fetch | 1 |
| `repo.py` | index fetch/merge, cache, GPG verification | 2 |
| `db.py` | installed DB, ownership index, locking | 2 |
| `resolver.py` | deps, provides, conflicts, replaces | 3 |
| `transaction.py` | stage → backup → commit → rollback | 3 |
| `cli.py` | command surface, JSON output, exit codes | 4 |
| `builder/` | deterministic packages + index | 5 |

## State

```
/var/lib/titanpkg/
├── installed.json          # authoritative DB
├── lock                    # flock target
├── txn/                    # per-transaction staging + backups
└── orphans.json            # optional future use

/var/cache/titanpkg/
├── index-<repo>.json       # cached repository indexes
└── <package>.tpkg          # verified package cache
```

## Transaction flow

1. Acquire exclusive `flock`.
2. Resolve the package set against the merged index + installed DB.
3. Preflight: allowed prefixes, pacman ownership, conflicts, disk space.
4. Download and verify every package (hash + signature).
5. Extract to a staging directory under `/var/lib/titanpkg/txn/<id>`.
6. Back up every path that will be replaced.
7. Commit files with `os.replace`; run scriptlets.
8. Write the DB last (atomic rename).
9. On any failure before step 8, restore backups and remove new files.
10. Release the lock.

## Versioning

`pkgver-pkgrel` with optional `epoch:`. `titanpkg` does not use semantic
versioning semantics; it uses pacman's ordering rules.

## Why Python

- Standard library only (`tarfile`, `gzip`, `hashlib`, `json`, `urllib`).
- Available in the ArchTitan package set.
- Easy to audit as root-executed code.
- No compiled artefacts to bootstrap on a fresh install.

If startup time or memory ever matters, the transaction core can be ported to
C++ behind the same on-disk formats.
