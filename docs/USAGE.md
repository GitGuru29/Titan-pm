# Usage

## Install the manager

The manager is part of the ArchTitan overlay. For development:

```bash
git clone https://github.com/YOURNAME/titanpkg
cd titanpkg
make dev          # creates .venv with dev deps
./bin/titanpkg --version
```

## Add a repository

```bash
sudo install -Dm644 config/repos.d/archtitan.json.example \
  /etc/titanpkg/repos.d/archtitan.json
sudoedit /etc/titanpkg/repos.d/archtitan.json   # set the server URL
```

Import the signing key once:

```bash
sudo titanpkg key import archtitan.gpg
titanpkg key list
```

## Daily use

```bash
titanpkg update                    # refresh indexes
titanpkg list --upgradable         # what can move?
sudo titanpkg upgrade              # upgrade everything installed
sudo titanpkg install titan-sysmon # add a package
titanpkg info titan-hwm            # versions, deps, files
titanpkg verify                    # check installed files
```

## Rollback

```bash
titanpkg info titan-hwm            # see available versions
sudo titanpkg rollback titan-hwm 3.1.0-1
```

Older packages are retained in the repository index and in the local cache
(`/var/cache/titanpkg`).

## Sandbox / image building

Every command honours `--root`:

```bash
titanpkg --root /tmp/chroot update
titanpkg --root /tmp/chroot install titan-ui
```

Scriptlets are skipped for non-`/` roots unless `--run-scripts` is passed.

## Troubleshooting

```bash
titanpkg doctor        # config, DB, keyring, repo reachability
titanpkg verify        # modified or missing files
titanpkg self-check    # the manager's own files
```

If a transaction is interrupted, the next run reports the unfinished transaction
and offers to roll it back.
