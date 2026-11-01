# Browser capability baseline

## Headless exact-origin UAT

For application UAT prefer the runner's schema-v3 `origin-uat` capability over
the persistent desktop Chrome profile. It uses an ephemeral profile, one exact
VM-loopback origin, and a lease-bound SSH reverse forward to the fixed local DEV
lane. It permits GET/HEAD/POST only and uploads only catalogued synthetic fixture
IDs whose expected SHA-256 is present in both the contract and installed
catalog. Privacy mode permits click/assert/file-fixture actions only and creates
no browser trace or screenshots. Do not use it with credentials, real
statements, a public tunnel, a second origin, a desktop file picker, or the
generic `run` command.

Enable the origin in a checkpoint-bound transaction, execute only through
`remote_computer.py run-origin-uat`, retain the browser/network evidence, and
roll the configuration back to `allow_network=false` with zero origins after
the batch.

Use one isolated Chrome profile for remote UAT. An extension being present is
not proof that its account is authenticated or that its bridge is ready.

## Integrations installed on the AI Lab UAT VM

| Runtime | Integration | Readiness proof | Activation boundary |
| --- | --- | --- | --- |
| Kimi Code | Kimi WebBridge extension plus loopback daemon | The extension and daemon versions match, `kimi-webbridge status` reports `running=true` and `extension_connected=true`, and the Chrome WebBridge canary passes. | Do not expose port 10086 beyond loopback. Do not copy browser profile data. |
| Claude Code | Official Claude in Chrome extension | The expected extension ID and manifest exist in the isolated profile, and a Chrome canary passes. | Installation is not activation. Mateusz must sign in and grant permissions interactively. Never automate credentials, MFA, cookies, or consent. |

Claude's official extension ID is `fcoeoabgfenejglbffodgkkbkcdhcgfn`.
Kimi WebBridge's extension ID is `fldmhceldgbpfpkbgopacenieobmligc`.

## Other agent runtimes

Do not invent or force-install a Chrome extension merely to make every runtime
look symmetric. Codex Desktop, ZCode/OpenCode, MiniMax, Antigravity, Qwen, and
Grok may use their own browser/computer-use capability or consume evidence from
this runner through Agent Fleet. Their readiness must be proven through their
own adapter and a bounded canary. Lack of a provider-supported Chrome extension
is not a reason to widen Chrome remote-debugging access.

## Verification

1. Require `remote_computer.py doctor --mode desktop` to return `READY`.
2. Run `examples/desktop-chrome-webbridge-canary.json` and retain its evidence.
3. While Chrome is open, require `kimi-webbridge status` to report matching
   daemon and extension versions with `extension_connected=true`.
4. After Chrome closes, require no Chrome processes, `exit_type=Normal`, and
   `exited_cleanly=true`.
5. For Claude, verify the official extension manifest separately. Report
   `INSTALLED / ACTIVATION_REQUIRED` until Mateusz completes interactive sign-in
   and a fresh-session capability probe succeeds.

Official references:

- <https://support.claude.com/en/articles/12012173-get-started-with-claude-in-chrome>
- <https://support.claude.com/en/articles/12902428-use-claude-in-chrome-safely>
- <https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn>
