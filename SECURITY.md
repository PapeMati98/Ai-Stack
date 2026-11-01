# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue for security problems. There is no bug bounty; reports are handled on a
best-effort basis.

## What this repository is

AI Stack is source code for a single operator's agent tooling. It is not a
hosted service and contains no credentials, accounts, tokens, model weights or
CLI binaries. Nothing in it reports usage or data back to its authors. Strings
that look like keys or tokens in tests (for example a JWT ending in
`signature`, or values such as `token-123`) are synthetic fixtures.

Client and project names in tests and examples (Northwind, Globex, Initech,
Umbrella, Contoso, Hyperion) are fictional placeholders.

## Before you run anything

Treat this code like any automation that drives other programs on your behalf:

- Read a component's README and scripts before running them. Start in a
  disposable user profile or virtual machine.
- Installers write under your home directory, for example `~/.agents` and the
  configuration directories of the CLIs they integrate with (such as `~/.codex`
  or `~/.claude`). Back those up first.
- The native Fleet launches third-party coding CLIs with **your** subscriptions
  and API keys. Those runs can consume quota or cost money, and they send
  prompts and files to the respective providers.
- Content read from mail, web pages, tickets or transcripts is treated as
  untrusted data, but no prompt-injection filter is complete. Keep write and
  send permissions narrow.

### Scripts that change system state

These only run when you invoke them explicitly. Review them first:

| Script | Effect |
|---|---|
| `global/codex/skills/rsi-helpdesk/scripts/install_schedule.ps1` | Registers a Windows scheduled task |
| `global/codex/skills/idea-maker/scripts/install_schedule.ps1` | Registers a Windows scheduled task |
| `global/tools/muse-acl-guard/install-*-task.ps1`, `muse_acl_guard.ps1` | Registers scheduled tasks and changes file ACLs for a sandbox user group |
| `global/tools/muse-acl-guard/muse_read_deny.ps1` | Adds DENY entries for a sandbox user group on your profile roots and on the configured drive roots (`drive_roots`); run by the task that `install-muse-read-deny-task.ps1` registers |
| `global/tools/chrome-relay/owner_autostart.py apply` | Creates or enables a logon scheduled task (`schtasks /Create`) for the Chrome relay |
| `global/tools/chrome-relay/uat.py`, `global/tools/browser-worker/private_state.py` | Set private ACLs (`SetFileSecurityW`) on their own state directories |
| `shared/agent-office/office_server_package.py` | Generates an install package with systemd units and root-run setup scripts for a Linux host; it writes files, and you run them |
| `shared/mcp/install-windows.ps1` | Adds MCP server entries to the configuration of the selected coding CLIs (backups under `~/.agents/backups`, `-DryRun` and rollback available) |
| `global/codex/skills/remote-computer-use/scripts/*.ps1` | Stage and deploy the VM runner to a remote test VM over SSH and configure it there; `install_skill.ps1` instead copies this skill into the local skill directories of your coding CLIs |
| `global/codex/skills/hermes-delegation/scripts/hermes_delegate.py install-remote` / `rollback-remote` | Installs or rolls back the remote worker on the configured SSH host |
| `global/tools/hidden-launch/Protect-ConsoleTasks.ps1 -Apply` | Rewrites the actions of the scheduled tasks you name to use the hidden launcher (backups kept) and restricts ACLs on its state directory |
| `global/tools/hidden-launch/Set-UnattendedConsoleHost.ps1` | Changes the current user's default console host in the registry (`HKCU\Console\%%Startup`, original values backed up) |
| `global/tools/mimo/install-adapter.ps1` | Installs a CLI wrapper and sets persistent user environment variables |
| `global/codex/skills/remote-computer-use/assets/vm-runner/scripts/*.ps1` | Configures a **dedicated test VM** (ACLs, scheduled tasks, desktop settings; expects pre-created accounts); never run on a workstation |
| `shared/grok-office-gateway/*.service.example` | Example systemd unit; installing it requires `sudo` |
| `global/codex/skills/email/scripts/mail_direct.py` | Sets private ACLs on its own artifact directories |

Installers and repair helpers remove only their own staging, backup and lock
files. Report any deletion outside those paths as a vulnerability.

## Excluded from this edition

The Microsoft 365 workspace MCP connector (`shared/mcp/m365-workspace`) is not
published: its pinned dependency `@softeria/ms-365-mcp-server@0.150.2` is
affected by upstream advisories (including `@azure/msal-node` and `zod`), and
the fix requires a migration that has not been validated. Documentation that
mentions this connector describes the private deployment.

## Limitations

There is no security certification, no qualification across all supported
CLIs or operating systems, and no production deployment guarantee. See
[Validation status](docs/VALIDATION_STATUS.md).
