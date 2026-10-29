# Security Policy

`titanpkg` runs as root. Treat it as part of the trusted computing base.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities that allow privilege escalation,
path traversal, signature bypass or arbitrary file writes. Report privately to
the maintainer listed in `pyproject.toml`, or use GitHub's private vulnerability
reporting once the repository is public.

Include:

- affected version / commit;
- a minimal reproduction;
- impact assessment;
- suggested fix, if any.

## Guarantees the implementation must uphold

1. **No unverified execution.** A package is never extracted or executed until
   its SHA-256 matches the index and its detached signature verifies.
2. **No path escape.** Absolute paths, `..`, NUL bytes, hardlinks and symlinks
   that resolve outside `allowed_prefixes` are rejected.
3. **No pacman ownership violation.** A path owned by a pacman package is never
   overwritten or removed.
4. **No implicit setuid.** Setuid/setgid bits are stripped unless declared in
   `setuid` and logged at install time.
5. **Atomic database.** The DB is written with `os.replace` after files commit;
   a crash leaves the old DB intact.
6. **Locked transactions.** Only one mutating process at a time.
7. **Dedicated keyring.** Repo keys never touch the user's `~/.gnupg`.

## Threat model

| Adversary | In scope? | Mitigation |
|---|---|---|
| Network attacker modifying downloads | Yes | HTTPS + GPG + SHA-256 |
| Compromised mirror | Yes | Same signatures; mirrors are untrusted |
| Malicious package author | Yes (they can sign) | Signed packages are trusted by definition; review manifests |
| Local unprivileged user racing the root process | Yes | `flock`, `os.replace`, no world-writable staging |
| Root-level attacker | No | Root can do anything |
| Compromised signing key | Out of scope | Rotation + revocation procedure |

## Signing key hygiene

- Use a dedicated key, not your personal GPG key.
- Prefer a hardware token or an offline key for releases.
- Publish the fingerprint in the repository README and in the ArchTitan wiki.
- Rotate with an overlap period: sign the index with both keys, then retire.
