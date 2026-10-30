# titanpkg

**A standalone package manager for ArchTitan OS subsystems — built to run alongside pacman.**

`titanpkg` installs, upgrades, verifies and rolls back first-party Titan packages
(`titan-hwm`, `titan-ui`, `titan-quicksettings`, `archtitan-guard`, …) without
touching the pacman database and without requiring a paid server.

> **Status:** scaffold / design phase. The interface, package format and repo
> layout below are the contract. Implementation lands in the phases described in
> [Implementation Plan](#implementation-plan).
>
> **Repository:** <https://github.com/GitGuru29/Titan-pm>

---

## Why another package manager?

ArchTitan ships a set of fast-moving, tightly coupled subsystems. Rebuilding the
ISO for every UI tweak is slow, and pushing Titan files through pacman creates two
problems:

1. **Ownership conflict** — pacman must own every file it installs, but Titan
   binaries currently live as prebuilt overlays inside `airootfs/`.
2. **Blast radius** — a bad overlay-only update can break the desktop with no
   per-package version, manifest, checksum or rollback.

`titanpkg` gives Titan components their own lifecycle:

| Concern | Owner |
|---|---|
| Kernel, drivers, base libraries, Qt, system packages | **pacman** |
| Titan daemons, HUDs, QML design system, themes | **titanpkg** |

`titanpkg` **never** edits `/var/lib/pacman`, never removes pacman-owned files, and
refuses to install over a path owned by a pacman package. It owns a separate
database, separate cache, separate signatures and a separate repository.

### Non-goals

- Replacing pacman or resolving Arch system dependencies.
- Partial upgrades of Arch packages.
- Managing AUR packages (use an AUR helper).
- A GUI store (a thin QML front-end may come later).

---

## Design principles

1. **Separate ownership.** The manager only touches paths it recorded as its own.
   Default prefixes are `/usr/local/**`, `/usr/lib/qt6/qml/ArchTitan/**`,
   `/usr/share/archtitan/**`, `/etc/archtitan/**`, `/etc/skel/**`,
   `/etc/systemd/system/**`, `/var/lib/archtitan/**`, `/opt/archtitan/**`.
2. **Signed by default.** Repository index and packages carry detached GPG
   signatures. `TrustAll` is opt-in and loud.
3. **Transactional.** Every install/upgrade/remove is staged, backed up and
   reversible. A failure restores the previous tree.
4. **No hidden shell.** Package scriptlets are explicit, declared in the package,
   and run with `TITANPKG_*` environment variables.
5. **Deterministic.** Same sources + same manifest => same package bytes
   (`SOURCE_DATE_EPOCH`, fixed uid/gid, sorted entries).
6. **Free to host.** The repo is static files: GitHub Pages, GitLab Pages,
   Codeberg Pages or Cloudflare R2. No server process, no database daemon.
7. **Arch-native.** `pkgver-pkgrel` versioning, `vercmp`-compatible comparison,
   and dependency strings that match pacman's syntax.

---

## Architecture

```
                 ┌──────────────────────────┐
                 │  static HTTPS repository │
                 │  titanpkg.db.json (+sig) │
                 │  x86_64/*.tpkg  (+sig)   │
                 └────────────┬─────────────┘
                              │ urllib / file://
┌─────────────────────────────▼──────────────────────────────┐
│                          titanpkg                          │
│                                                            │
│  repo.py       fetch + verify index                        │
│  resolver.py   deps, provides, conflicts, version compare  │
│  transaction.py stage → backup → commit → rollback         │
│  db.py         /var/lib/titanpkg/installed.json            │
│  vercmp.py     Arch-compatible version ordering            │
│  cli.py        update/install/upgrade/remove/verify/...    │
└─────────────────────────────┬──────────────────────────────┘
                              │ writes only owned paths
                              ▼
        /usr/local/bin, /usr/local/lib/archtitan,
        /usr/lib/qt6/qml/ArchTitan, /etc/archtitan, …
```

### Package format (`.tpkg`)

A gzip-compressed tar (stdlib only, no external compressor needed):

```
titan-hwm-3.1.0-1-x86_64.tpkg
├── METADATA.json     # name, version, deps, conflicts, backup list, setuid list
├── FILES.json        # per-file sha256 + mode, or symlink target
├── INSTALL           # optional scriptlets (pre_install, post_upgrade, …)
└── payload/
    └── usr/local/bin/titan-hwm
```

Full specification: [`docs/PACKAGE-FORMAT.md`](docs/PACKAGE-FORMAT.md).

### Repository layout

```
repo/
├── .nojekyll
├── titanpkg.db.json          # merged index
├── titanpkg.db.json.sig      # detached GPG signature
└── x86_64/
    ├── titan-hwm-3.1.0-1-x86_64.tpkg
    ├── titan-hwm-3.1.0-1-x86_64.tpkg.sig
    └── ...
```

Full specification: [`docs/REPOSITORY.md`](docs/REPOSITORY.md).

---

## CLI surface

`titanpkg` supports **Arch `pacman`-style flags** (`-Syu`, `-S`, `-Rns`, `-Q`, `-Qo`, `-Qk`) as well as readable subcommands:

```text
# Arch pacman-style flags
titanpkg -Syu [pkg…]                sync repository indexes and upgrade all subsystems
titanpkg -S <pkg…>                  install (or reinstall) packages (+ dependencies)
titanpkg -Sy                        refresh repository indexes only
titanpkg -Su [pkg…]                 upgrade installed subsystems
titanpkg -Ss <term>                 search package names and descriptions
titanpkg -Si <pkg> / -Qi <pkg>      metadata, versions, dependencies
titanpkg -Sl                        list all available repository packages
titanpkg -Sc / -Scc                 prune old (-Sc) or all (-Scc) cached packages
titanpkg -R <pkg…> / -Rns <pkg…>    remove packages (and orphaned deps with -s)
titanpkg -Q                         list installed packages
titanpkg -Qu                        list upgradable packages
titanpkg -Ql <pkg>                  list package file manifest
titanpkg -Qo <path>                 query which Titan package owns a file
titanpkg -Qk [pkg…]                 verify installed files against SHA-256 hashes
titanpkg -Qdt                       list orphaned dependency packages

# Equivalent subcommands
titanpkg update                     refresh repo indexes
titanpkg upgrade [pkg…]             upgrade all or selected installed packages
titanpkg install <pkg…>             install packages (+ dependencies)
titanpkg reinstall <pkg…>           reinstall the installed version
titanpkg remove <pkg…>              remove packages (reverse-dep check)
titanpkg autoremove                 remove orphaned dependencies
titanpkg list [--installed|--available|--upgradable|--orphans]
titanpkg search <term>              search name/description
titanpkg info <pkg>                 metadata, versions, files
titanpkg files <pkg>                list package file manifest
titanpkg owns <path>                query which package owns a path
titanpkg verify [pkg…]              check installed files against hashes
titanpkg rollback <pkg> [version]   install a previous version
titanpkg clean [--all]              prune cached packages
titanpkg doctor                     config/db/repo/keyring diagnostics
titanpkg key import <file>          add a trusted repository key
titanpkg key list                   list trusted keys
titanpkg self-check                 verify the manager's own files
```

Global flags: `--root PATH` (sandbox/test), `--noconfirm` (`-y`), `--needed`,
`--dry-run` (`-p`), `--offline`, `--verbose` (`-v`), `--json`.

Example:

```bash
sudo titanpkg -Syu
sudo titanpkg -S titan-sysmon
sudo titanpkg -Rns titan-sysmon
titanpkg -Qu
titanpkg -Qo /usr/local/bin/titan-hwm
titanpkg -Qk
sudo titanpkg rollback titan-hwm 3.1.0-1
```

---

## Configuration

`/etc/titanpkg/titanpkg.json`:

```json
{
  "db": "/var/lib/titanpkg",
  "cache": "/var/cache/titanpkg",
  "config_dir": "/etc/titanpkg",
  "repos_dir": "/etc/titanpkg/repos.d",
  "keyring": "/etc/titanpkg/trusted.gpg",
  "siglevel": "required",
  "timeout": 30,
  "allowed_prefixes": [
    "usr/local/",
    "usr/lib/qt6/qml/ArchTitan/",
    "usr/share/archtitan/",
    "etc/archtitan/",
    "etc/skel/",
    "etc/systemd/system/",
    "var/lib/archtitan/",
    "opt/archtitan/"
  ]
}
```

`/etc/titanpkg/repos.d/archtitan.json`:

```json
{
  "name": "archtitan",
  "server": "https://YOURNAME.github.io/titanpkg-repo",
  "enabled": true,
  "siglevel": "required"
}
```

---

## Free hosting

The repository is just static files, so it fits free tiers:

| Host | Free tier | Notes |
|---|---|---|
| **GitHub Pages** | ~1 GB repo, ~100 GB/mo | Recommended; deploy from Actions |
| GitLab Pages | 10 GB repo | Same static model |
| Codeberg Pages | generous | No CI minutes needed |
| Cloudflare R2 | 10 GB storage, zero egress | Best if packages grow |
| Backblaze B2 + Cloudflare | 10 GB storage | Free egress through CF |

A ready-to-use workflow lives at
[`.github/workflows/repo.yml`](.github/workflows/repo.yml). It builds packages,
regenerates the index, signs it, and deploys to Pages on every tag.

**Gotcha handled by the builder:** `repo-add`-style `.db` files are symlinks on
disk; the workflow dereferences them before publishing because Pages does not
serve symlinks.

---

## Security model

- Detached GPG signatures for the index **and** every package.
- Trusted keys stored in a dedicated keyring (`/etc/titanpkg/trusted.gpg`),
  not the user's `~/.gnupg`.
- Package payload paths are validated against the allowed-prefix list; `..`,
  absolute paths and escaping symlinks are rejected.
- Setuid/setgid bits are stripped unless the path is explicitly listed under
  `setuid` in `METADATA.json`.
- Files owned by pacman are never overwritten; the transaction aborts with the
  owning package name.
- Scriptlets run only from signed packages and are skipped in `--root` sandboxes.

See [`SECURITY.md`](SECURITY.md) and [`docs/SECURITY.md`](docs/SECURITY.md).

---

## Repository structure

```text
titanpkg/
├── README.md
├── LICENSE
├── SECURITY.md
├── CONTRIBUTING.md
├── CHANGELOG.md
├── pyproject.toml
├── Makefile
├── src/titanpkg/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── db.py
│   ├── errors.py
│   ├── repo.py
│   ├── resolver.py
│   ├── transaction.py
│   ├── vercmp.py
│   ├── util.py
│   ├── core/            # shared models (package, dependency, manifest)
│   └── builder/         # package + index builders
├── tests/
│   ├── unit/
│   └── integration/
├── packaging/
│   ├── manifests/       # ArchTitan package manifests (*.json)
│   ├── scripts/         # .install scriptlets
│   ├── build-package.py
│   ├── build-repo.py
│   └── build-packages.sh
├── config/              # shipped defaults
├── docs/
├── examples/
├── bin/titanpkg         # development launcher
└── .github/workflows/
    ├── ci.yml
    └── repo.yml
```

---

## Implementation plan

Detailed breakdown: [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md).

### Phase 0 — Scaffold (this commit)

- [x] Repository layout, packaging metadata, license, security policy.
- [x] README with architecture, package format, repo model and CLI contract.
- [x] CI and Pages workflow skeletons.
- [x] Example manifest, config and launcher.

### Phase 1 — Core data model

- [x] `vercmp.py`: Arch-compatible `epoch:pkgver-pkgrel` comparison + tests.
- [x] `core/models.py`: `Package`, `FileEntry`, `Dependency`, `Transaction`.
- [x] `config.py`: root-aware paths, config/repo parsing, env overrides.
- [x] `util.py`: atomic writes, hashing, safe path handling, URL fetch.
- [x] Unit tests for version ordering, dependency parsing and safe paths.

### Phase 2 — Repository + database

- [x] `repo.py`: fetch/merge indexes, cache, signature verification.
- [x] `db.py`: installed database, atomic updates, file ownership index.
- [x] `key` management (`titanpkg key import/list`).
- [x] `doctor` reachability/integrity checks.
- [x] Tests with a local `file://` repository.

### Phase 3 — Transactions

- [x] `resolver.py`: dependencies, provides, conflicts, replaces, system deps.
- [x] `transaction.py`: staging, backups, commit, rollback, `.pacnew`/`.pacsave`.
- [x] Scriptlet runner with environment contract.
- [x] Setuid policy and allowed-prefix enforcement.
- [x] Integration tests: install → upgrade → downgrade → remove → verify.

### Phase 4 — CLI

- [x] `cli.py` with the full command surface, `pacman`-style `-Syu`/`-S`/`-R`/`-Q` flags, and JSON output.
- [x] Confirmation prompts, dry-run, non-interactive mode.
- [x] Shell completions (bash/zsh/fish).
- [x] Man page.

### Phase 5 — Packaging pipeline

- [x] `builder/build_package.py`: deterministic `.tpkg` creation.
- [x] `builder/build_repo.py`: index regeneration + signing.
- [x] Manifests for every ArchTitan subsystem that has source (+ `titanpkg` self-hosting).
- [x] `build-packages.sh` with dependency ordering.
- [ ] Source builds (`DESTDIR`) instead of copying `airootfs` artefacts.

### Phase 6 — Publishing

- [ ] GitHub Pages workflow with optional GPG signing secrets.
- [ ] `.nojekyll`, stable `repos.d` URL, keyring bootstrap instructions.
- [ ] Release tagging + `pkgver` automation.
- [ ] Mirror support (multiple `Server` entries).

### Phase 7 — ArchTitan integration

- [ ] Ship `titanpkg` + default config in the ISO overlay.
- [ ] Add `[archtitan]` repo config for installed systems.
- [ ] Convert overlay binaries into packages (remove duplicated `/usr/bin` copies).
- [ ] post-upgrade hooks: `systemd daemon-reload`, restart THM, reload Waybar.
- [ ] `titan update` convenience wrapper if desired.

---

## Testing

```bash
make test          # unit + integration
make lint          # ruff + mypy
make build         # build .tpkg files from manifests
make repo          # regenerate index into dist/repo
```

The integration suite creates a throwaway `--root`, builds two packages, publishes
a `file://` repo, then exercises install/upgrade/rollback/remove/verify without
root privileges.

---

## Compatibility

- Linux (uses `flock`, `os.geteuid`, `/proc` for diagnostics).
- Python **3.9+**, standard library only at runtime.
- Package payloads are architecture-tagged (`x86_64` today).

---

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Security reports: [`SECURITY.md`](SECURITY.md).

## License

Apache-2.0. See [`LICENSE`](LICENSE).

---

## Status board

| Area | State |
|---|---|
| Design / contract | ✅ documented |
| Core manager (`-Syu`, transactions, resolver, DB) | ✅ implemented |
| Repository & `.tpkg` builder | ✅ implemented |
| ArchTitan manifests (18 packages) | ✅ implemented |
| Free hosting workflow | ✅ ready |
| Tests (unit + integration) | ✅ passing |
