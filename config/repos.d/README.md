# Repository definitions

Each `*.json` file describes one repository. Files are loaded in lexicographic
order; the first repository to provide a `name+version` wins.

```json
{
  "name": "archtitan",
  "server": "https://YOURNAME.github.io/titanpkg-repo",
  "enabled": true,
  "siglevel": "required",
  "servers": [
    "https://YOURNAME.github.io/titanpkg-repo",
    "https://mirror.example.org/titanpkg"
  ]
}
```

- `server` — primary base URL. `file:///path/to/repo` is supported.
- `servers` — optional ordered mirrors; tried when `server` fails.
- `siglevel` — `required` (default), `optional`, or `none`.
- `enabled` — set to `false` to keep the file but skip the repo.

The index is fetched from `<server>/titanpkg.db.json`.
