# OpenClaw Windows owned stdio — pinned corrective patch

Three-module correction for installed OpenClaw 2026.9.4. The direct Windows adapter did not expose the descendant-extinction witness required by owned stdio. The existing Windows Job anchor also supplied NUL stdin, so simply selecting it could not serve MCP.

The patch uses the existing kill-on-close Job ownership mechanism and adds a bounded asynchronous stdin pipe/IPC bridge. It does not replace the fail-closed cleanup gate. The sandbox core, WSL/gateway, OpenClaw data, credentials and network configuration remain outside its scope. No vendor source copies or runtime binaries are included here.

`pins.json` binds all three original and accepted output hashes. The CLI permits only the pinned native dist. `plan`/`verify` perform no writes; `apply --receipt <absolute task evidence path>` verifies every source, generated output and retained preimage before writing. Reapplying an unchanged patch returns CURRENT. `rollback --receipt <same path>` validates every preimage/destination before restoring exact original bytes. Files, volumes and old releases are retained.

Candidate code/security: independent Gemini 4 Argon ACCEPT, native run run-20261014T073028Z-1dbefcf886. Actual native Job descendant extinction and MCP/large Unicode/EOF cleanup PASS; full local-Qwen UAT is mandatory before activation closeout. Evidence is maintained in the existing activation task, not a new process registry.
