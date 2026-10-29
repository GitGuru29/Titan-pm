# Implementation Plan

This document is the build order for `titanpkg`. Each phase is shippable and
tested before the next one starts. The manager must remain usable at every step;
no phase may leave the installed database in an ambiguous state.

---

## Definitions

- **Package** — a signed `.tpkg` archive containing metadata, a file manifest and
  a payload tree.
- **Repository** — a static HTTP(S) or `file://` directory containing an index
  (`titanpkg.db.json`) and architecture subdirectories of packages.
- **Transaction** — one atomic operation that installs, upgrades, downgrades or
  removes a set of packages.
- **Owned path** — a filesystem path recorded in
  `/var/lib/titanpkg/installed.json` as installed by `titanpkg`.
- **System package** — a package owned by pacman. `titanpkg` must never modify or
  remove system packages.

---

## Phase 0 — Scaffold

**Goal:** a repository that documents the contract and can be pushed.

Deliverables:

- Directory layout, license, contributing and security policies.
- README covering architecture, package format, repository layout, CLI surface,
  configuration, free hosting and this plan.
- CI workflow that runs the (initially trivial) test suite.
- Pages workflow skeleton with the `.nojekyll` + index-dereference gotcha noted.
- Example package manifest, install scriptlet, default config and repo config.
- Development launcher (`bin/titanpkg`) that works without installation.

Acceptance:

- `make test` succeeds.
- `titanpkg --version` runs from a checkout.
- `docs/` describes the format well enough for a contributor to write a builder.

---

## Phase 1 — Core data model

**Goal:** tested primitives with no I/O side effects.

Files: `vercmp.py`, `core/models.py`, `util.py`, `config.py`, `errors.py`.

### 1.1 Version comparison

Implement `vercmp(a, b)` compatible with libalpm:

- `epoch:pkgver-pkgrel` splitting (`epoch` defaults to `0`, `pkgrel` to `0`).
- Alternating numeric/alpha segment comparison.
- Numeric segments compare by length after stripping leading zeros, then
  lexicographically.
- Numeric segments sort newer than alpha segments.
- `~` sorts older, `^` sorts newer.
- Return `-1`, `0`, `1`.

Tests must cover the cases in `pactest`/`vercmp` documentation, including
`1.0` vs `1.0-1`, `1.0a` vs `1.0`, `1.0~rc1` vs `1.0`, and epoch ordering.

### 1.2 Models

- `Dependency(name, op, version)` with parser for `name`, `name>=1.2`,
  `name=1.2-3`, `name<2`.
- `FileEntry(path, type, sha256, mode, target)` for regular files and symlinks.
- `PackageMetadata` mirroring `METADATA.json`.
- `InstalledPackage(version, repo, reason, installed_at, files)`.
- Validation for names, versions and architecture.

### 1.3 Utilities

- Atomic write (`tempfile` + `fsync` + `os.replace`).
- `sha256_file`, `sha256_bytes`.
- Safe path handling: reject absolute paths, `..`, empty segments, NUL bytes.
- Allowed-prefix matcher.
- URL fetch with redirects, timeout, `file://` support and a User-Agent.

### 1.4 Configuration

- Root-aware `Paths` object (`--root` / `TITANPKG_ROOT`).
- Load `/etc/titanpkg/titanpkg.json` over defaults.
- Load `repos.d/*.json` in lexicographic order.
- Environment overrides for CI and tests.

Acceptance:

- 100% unit coverage of `vercmp` and dependency parsing.
- No module performs filesystem writes at import time.
- `python -m titanpkg --version` works with a custom root.

---

## Phase 2 — Repository and database

**Goal:** read and write state safely.

### 2.1 Repositories

- Fetch `<server>/titanpkg.db.json` with ETag/If-Modified-Since cache support.
- Verify detached signature (`titanpkg.db.json.sig`) against
  `/etc/titanpkg/trusted.gpg`.
- Merge multiple repositories: first repo in sorted order wins on duplicate
  `name+version`; `latest` is recomputed with `vercmp`.
- Cache indexes in `/var/cache/titanpkg/index-<repo>.json`.
- Support `--offline` (use cache only) and `file://` servers.

### 2.2 Package download

- Download to `/var/cache/titanpkg/<filename>.part`, verify SHA-256, then rename.
- Verify `<filename>.sig` if the repo `siglevel` requires it.
- Never execute or extract a package whose hash or signature failed.
- Resume-friendly cache keyed by filename + hash.

### 2.3 Installed database

`/var/lib/titanpkg/installed.json`:

```json
{
  "format": 1,
  "generated": "2026-01-01T00:00:00Z",
  "packages": {
    "titan-hwm": {
      "version": "3.1.0-1",
      "repo": "archtitan",
      "reason": "explicit",
      "installed_at": "2026-01-01T00:00:00Z",
      "files": {
        "usr/local/bin/titan-hwm": {
          "type": "file",
          "sha256": "…",
          "mode": "0755",
          "written": true,
          "backup": false
        }
      }
    }
  }
}
```

- Atomic save; preserve unknown top-level keys for forward compatibility.
- Maintain a reverse index `path -> package` in memory.
- Detect corruption and refuse to write over a broken database.

### 2.4 Keyring

- `titanpkg key import <file>` uses a dedicated keyring, never the user's.
- `titanpkg key list` prints fingerprints and UIDs.
- `doctor` warns when `siglevel=optional` or no keys are present.

Acceptance:

- Install from a `file://` repository in a temporary root.
- Corrupt a package and confirm it is rejected before extraction.
- Remove the cache and confirm the index can be re-fetched.

---

## Phase 3 — Transactions

**Goal:** correct, reversible mutations.

### 3.1 Resolver

- Input: requested package names, merged index, installed DB, pinned versions.
- Recursively resolve `depends`; satisfy via package name or `provides`.
- Version constraints use `vercmp`.
- Detect dependency cycles and report the chain.
- `conflicts` and `replaces` are evaluated before any file is written.
- `upgrade` considers installed packages whose latest version is newer.
- Optional `system_depends` entries are checked against `pacman -Q` when pacman
  is available (read-only, never modifies pacman).

### 3.2 Transaction lifecycle

```
acquire lock
  plan      → resolved package set + file operation list
  preflight → allowed prefixes, conflicts, pacman ownership, disk space
  download  → cache + verify
  stage     → extract to /var/lib/titanpkg/txn/<id>/stage
  backup    → copy every path that will be replaced
  commit    → move files into place, run pre/post scriptlets, update DB
  rollback  → restore backups, remove new files, restore DB (on any failure)
release lock
```

- Use a single global lock (`flock`) for all mutations.
- File replacement is `copy to temp in same directory` + `os.replace`.
- Symlinks are recreated, not followed.
- Empty directories are removed on uninstall, but only if they are under an
  allowed prefix and empty.
- Backups are retained until the next successful transaction; `clean` prunes
  old transaction directories.

### 3.3 Config files

- Paths listed in `backup` are treated like pacman conffiles.
- On upgrade: if the on-disk file differs from the recorded hash, write the new
  file as `<path>.pacnew` and leave the user's file untouched.
- On remove: if modified, rename to `<path>.pacsave`; otherwise delete.
- Recorded hash always describes the package's intended content; `written: false`
  marks files superseded by `.pacnew`.

### 3.4 Scriptlets

Supported functions in `INSTALL`:

```
pre_install post_install pre_upgrade post_upgrade pre_remove post_remove
```

- Executed with `/bin/sh`, cwd set to `/`, and a clean environment plus
  `TITANPKG_PACKAGE`, `TITANPKG_VERSION`, `TITANPKG_ROOT`, `TITANPKG_ACTION`.
- Skipped when running under a non-`/` root unless `--run-scripts` is passed.
- Output is streamed; exit status is reported but does not silently roll back a
  completed commit (matches pacman's behaviour for post-install hooks).

Acceptance:

- Integration test: install v1, modify a conffile, upgrade to v2, assert
  `.pacnew`, remove and assert `.pacsave`.
- Simulated write failure rolls back every file and the DB.
- Killing the process mid-transaction leaves either the old or new DB, never a
  half-written JSON.

---

## Phase 4 — CLI

**Goal:** the complete user-facing surface.

- `argparse` with subcommands and a stable JSON mode (`--json`).
- Confirmation prompt showing the plan; `--noconfirm` for automation.
- `--dry-run` prints the plan and exits before downloading.
- Exit codes: `0` success, `1` generic, `2` usage, `3` resolution, `4` security,
  `5` transaction.
- Completions for bash, zsh and fish.
- Man page generated from a single source of truth.

Acceptance:

- Every command has a `--help` example.
- `--json` is machine-parseable and documented.
- No command touches the filesystem without the lock (except read-only queries).

---

## Phase 5 — Packaging pipeline

**Goal:** turn any Titan project into a reproducible `.tpkg`.

- `builder/build_package.py`:
  - Reads a manifest (name, version, release, arch, deps, files/globs).
  - Stages files from a source root (later: runs the project's build with
    `DESTDIR`).
  - Computes hashes and modes.
  - Writes deterministic tar entries (`mtime=SOURCE_DATE_EPOCH`, uid/gid 0,
    sorted names).
  - Emits `{name}-{version}-{release}-{arch}.tpkg`.
- `builder/build_repo.py`:
  - Copies packages to `repo/<arch>/`.
  - Builds `titanpkg.db.json` with all versions (latest computed by `vercmp`).
  - Signs the index and (optionally) each package.
  - Writes `.nojekyll` and dereferences index copies for Pages.
- Manifests:
  - `titan-ui` first (QML module + shared library).
  - `archtitan-themes`, `archtitan-guard`.
  - `titan-hwm`, `titan-sysmon`, `titan-quicksettings`, `titan-media-hud`,
    `titan-spotlight`, `titan-powermenu`, `titan-workspace-hud`,
    `titan-hotzones`, `titan-menu`, `titan-settings`, `titanfetch`,
    `titan-browser`.
  - `titanpkg` self-hosting package.

Acceptance:

- Building the same source twice produces byte-identical packages.
- The generated index validates against the schema.
- `titanpkg install` works end-to-end from a generated local repo.

---

## Phase 6 — Publishing

**Goal:** zero-cost, reproducible releases.

- GitHub Actions on tag:
  - Build packages with the pinned Python version.
  - Import a signing key from repository secrets (optional but recommended).
  - Regenerate the index.
  - Deploy `dist/repo` to GitHub Pages.
- Fail the release if `siglevel=required` and signing is unavailable.
- Produce a `SHA256SUMS` and attach it to the GitHub Release.
- Document key bootstrap and fingerprint verification for users.
- Support mirror lists in `repos.d` (`servers: [url1, url2]`).

Acceptance:

- A tag produces a browsable Pages URL with index, packages and signatures.
- A fresh machine can add the repo, import the key and install a package.

---

## Phase 7 — ArchTitan integration

**Goal:** ship the manager in the OS and stop distributing overlay binaries.

- Add `titanpkg` + default config to the ISO overlay.
- Add the `[archtitan]` repo stanza and keyring bootstrap to installed systems.
- Convert current `airootfs` binaries/configs into packages.
- Remove duplicate `/usr/bin` and `/usr/local/bin` copies.
- Add post-upgrade scriptlets:
  - `systemctl daemon-reload`
  - `systemctl try-restart titan-hwm.service`
  - `hyprctl reload` / Waybar reload where appropriate
- Keep `archtitan-guard` hashes in sync, or teach it to verify package manifests.
- Document `titanpkg` in the ArchTitan wiki.

Acceptance:

- A clean ArchTitan install can update a Titan subsystem with `titanpkg` without
  touching pacman.
- `pacman -Qo` never reports a file owned by both managers.
- Removing a Titan package leaves the base system intact.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Version comparison drifts from pacman | Port libalpm cases; cross-check with `vercmp` in CI |
| Partial transaction leaves broken desktop | Stage + backup + rollback; DB written last |
| Path traversal via crafted package | Reject `..`, absolute paths, escaping symlinks |
| Pacman file ownership collision | Preflight `pacman -Qo`; default to `/usr/local` |
| Signature key compromise | Separate signing key, documented rotation, short expiry |
| Free host bandwidth limits | Cloudflare R2 fallback; small packages |
| Python 3.9 compatibility | CI matrix 3.9 + latest |
| Repo index drift | Index generated only by `build_repo.py`, never hand-edited |

---

## Release checklist

1. All phases' acceptance tests pass.
2. `make lint && make test` is green on 3.9 and latest.
3. Version bumped in `pyproject.toml` and `CHANGELOG.md`.
4. Packages rebuilt from a clean checkout.
5. Index signed and deployed to Pages.
6. Fresh-machine smoke test: add repo → import key → install → upgrade →
   rollback → remove.
7. ArchTitan wiki updated.
