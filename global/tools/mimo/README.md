# MiMo Code on AILAB

Native package: `@mimo-ai/mimocode-windows-x64@0.1.15`, official Xiaomi npm registry package. Installation verified SHA-512 and copied only the native binary and package metadata; no lifecycle scripts or remote install scripts ran.

The source adapter is `launch.ps1`; deploy with `install-adapter.ps1 -Apply`. It creates `~/.local/bin/mimo.cmd` and a managed launcher, preserving previous files as dated backups. No deletion or credential mutation is performed by the installer. Authentication uses native `mimo auth login --provider xiaomi` and the owner's Token Plan selection in the browser. Credentials belong only in `~/.mimocode/data/auth.json`, never in repository evidence.

The launcher pins the executable hash and version, disables telemetry, sharing, updates, pruning, unrequested plugins, and inherited Claude MCP. It preserves standard discovery of `~/.agents/skills`. Workspace permissions and global instructions are managed in MiMo config; native grants remain required for interactive actions. Fleet uses a separate constrained per-run configuration.

Native startup also imports Claude history unless `MIMOCODE_DISABLE_CLAUDE_IMPORT=true`; the launcher explicitly disables this. Initial onboarding runs preceded discovery of this independent switch and contain local imported history. They are not distributable evidence or backup inputs. Native startup would delete `mimo.exe.old_*` upgrade leftovers; the launcher refuses startup if any exist, preserving them for owner review.

`mimocode.json` is the AILAB source config, deployed with a dated backup to `~/.mimocode/config/mimocode.json`. It selects V2.6 Pro explicitly (including auxiliary calls), loads the authoritative shared AGENTS.md, disables sharing and snapshots, and retains native confirmation for deletion and access outside the workspace. On another host the instruction path must be configured explicitly. Login completion and a real Token Plan run are separate acceptance checks.

Requires PowerShell 7 at `C:/Program Files/PowerShell/7/pwsh.exe` and `~/.local/bin` on PATH (otherwise use the full command path). The installer also persists four user environment settings: `MIMOCODE_ENABLE_ANALYSIS=false`, `MIMOCODE_DISABLE_SHARE=true`, `MIMOCODE_DISABLE_AUTOUPDATE=true`, `MIMOCODE_DISABLE_PRUNE=true`. Dry-run and apply report before/after values; dated install receipts preserve them for rollback. Existing launchers have dated backups. Pruning and rotation are disabled, so storage cleanup requires a separate owner-authorized operation.

Token Plan Standard costs USD16/month at onboarding, with 11 billion Credits/month. Credits are not tokens or USD. The requested worker is MiMo V2.6 Pro, never `mimo-auto`, Flash or a PAYG fallback. Remaining quota is unknown without current provider evidence.

Privacy evidence 2026-09-22: https://privacy.mi.com/XiaomiMiMoPlatformos/en_GB/ explicitly says content submitted through API/MiMo Code is not used for model training. This is the provider's declaration, not independent technical verification. Local analysis telemetry defaults on upstream and is explicitly off in this adapter. No project-context feedback or bug reports are sent.

Installation evidence and tests: `docs/mimo-onboarding-20260922/`. Native package provenance does not establish a complete binary security audit; the public tag contains plugin/mimo.ts and auth/index.ts, while binary equivalence has not been independently established.
