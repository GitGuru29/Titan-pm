# Free Hosting

A `titanpkg` repository is a directory of static files. You do not need a
server, a database or a paid plan.

## GitHub Pages (recommended)

1. Create a public repository, e.g. `titanpkg-repo`.
2. Add the workflow from `.github/workflows/repo.yml`.
3. Enable **Settings → Pages → Source: GitHub Actions**.
4. Push a tag (`v1.0.0`) or run the workflow manually.
5. Your repo base URL is:

   ```
   https://YOURNAME.github.io/titanpkg-repo
   ```

6. Install on a machine:

   ```bash
   sudo install -Dm644 archtitan.json /etc/titanpkg/repos.d/archtitan.json
   sudo titanpkg update
   sudo titanpkg install titan-hwm
   ```

Limits (soft): ~1 GB repository size, ~100 GB/month bandwidth. Titan packages
are a few MB, so this is plenty.

## GitLab Pages

Same static model. Publish the `dist/repo` directory as the Pages artifact. URL:

```
https://YOURNAME.gitlab.io/titanpkg-repo
```

## Codeberg Pages

Create a `pages` branch containing the repo files. URL:

```
https://YOURNAME.codeberg.page/titanpkg-repo
```

## Cloudflare R2

Best if the repo grows or you need guaranteed bandwidth:

- 10 GB storage free.
- **Zero egress fees.**
- Put a custom domain or `r2.dev` URL in `repos.d`.

## Backblaze B2 + Cloudflare

- 10 GB storage free.
- Free egress through Cloudflare (Bandwidth Alliance).
- Useful as a mirror.

## What not to use

- **GitHub Releases** as the primary `Server`: asset URLs change per release.
  A stable base exists (`releases/latest/download`) only if every asset is
  re-uploaded to each release. Pages is simpler.
- **A VPS**: unnecessary for static files.

## Signing keys in CI

Store the private key as a repository secret, import it in the workflow, and
sign the index. Never commit the key.

```bash
echo "$GPG_PRIVATE_KEY" | gpg --batch --import
./packaging/build-repo.py --repo dist/repo --packages dist/packages \
  --sign-key "$GPG_KEY_ID"
```

Publish the public key once at a stable URL alongside the repo, and document the
fingerprint so users can verify it out of band.
