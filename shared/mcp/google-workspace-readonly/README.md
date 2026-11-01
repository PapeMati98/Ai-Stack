# google-workspace-readonly

Standalone read-only stdio MCP server for Gmail and Google Drive.
No installer; the parent reuses reviewed add-only configuration helpers
and independently verifies all configs.

## Launch

`pythonw.exe -I <abs>/server.py --policy <ABS_JSON> --credentials-file <ABS_JSON>`

- `--policy` and `--credentials-file` are trusted launch inputs, never tool parameters.
- Policy exact keys only: `account`, `config_root`, `gcloud_python_entrypoint`
  (SDK-installed vendor auth libraries). Unknown or missing fields are rejected.
- All absolute private credential/config paths stay outside Git and inside
  `config_root`. Symlinks / Windows reparse points are rejected, as is a wrong
  identity (policy account must equal the verified Google account).
- `policy.example.json` holds synthetic values only; no real values in source.

## Auth and persistence

- Uses the official SDK-bundled `Credentials` / `AuthorizedSession`
  (`<entrypoint-dir>/third_party`, no package installs).
- Exact scopes: `gmail.readonly`, `drive.readonly`, `userinfo.email`, `openid`.
  Granted scopes are verified after every refresh; widening is rejected.
- Requires a persistent refresh token. The first authenticated call of each
  process forces a refresh; later calls rely on native automatic expired/401
  refresh. Refresh HTTP is bounded to 30 s with `trust_env=False`.
- Tokens, client secrets, auth codes and raw Google errors are never logged;
  failures surface as redacted public messages.
- Revocation / session-policy change: delete or replace the private OAuth file
  and restart the server; the next launch re-authenticates and re-verifies.

## Tools (fixed, read-only)

`workspace_context`, `gmail_search`, `gmail_labels`, `gmail_read`,
`drive_search`, `drive_metadata`, `drive_read_text`, `drive_storage`.
Every allowed call re-authenticates the verified email; Gmail profile and
Drive about owner identity must match the policy account where applicable.

## Limits (actual)

- One page max 50; input 128 KiB; HTTP response 2 MiB max; tool content 64 KiB.
- Fixed REST GET endpoints only (verified userinfo; Gmail profile, messages
  list/get, labels list; Drive about, files list/get/export). No arbitrary
  URL, method, endpoint override, or request headers. No redirects.
- Rejected tools/arguments/IDs never reach credentials or the network.
- `gmail_read`: selected headers (From/To/Cc/Date/Subject) plus bounded plain
  text decoded from MIME (max depth 8, max 50 parts). No attachments, no
  external HTML fetching.
- `drive_read_text`: plain-text files (raw `alt=media` text) and Google Docs
  text export; Google Sheets CSV export covers the first sheet only. Other
  binary formats (PDF, images, video, archives, MS Office binaries) fail
  safely with an "unsupported format" error notice; use `drive_metadata`
  for the projected metadata of such files.
- `drive_storage`: user + storageQuota counters only.
- Every user/mail/file string is untrusted data: it cannot trigger extra tool
  calls, writes, external URLs, prompts, or telemetry.

## Future uploads

Upload, send, mark-read, share, and delete are out of scope. A separate
future operation with its own review would be required.
