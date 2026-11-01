# Runtime and security contract

Verified against OpenAI documentation on 2026-08-26:

- Site tools: https://learn.chatgpt.com/docs/webmcp
- Desktop usage: https://help.openai.com/en/articles/20001423-using-site-tools-in-the-chatgpt-desktop-app
- Proposed WebMCP specification: https://webmachinelearning.github.io/webmcp/

## Capability matrix

| Runtime | Discover and execute | Implement or review page code |
|---|---:|---:|
| ChatGPT desktop built-in browser, Sol/Terra | yes, subject to rollout | yes |
| ChatGPT desktop built-in browser, Luna | no | yes |
| Chrome/Edge, including AI Lab UAT VM | no ChatGPT Site-tools execution | yes |
| Codex/Claude/Kimi/Grok/Qwen CLI | no native page discovery | yes |
| Enterprise or Edu workspace | no | yes |

The built-in browser owns a separate browser state. A Chrome login does not
automatically appear there. Tools disappear after closing or navigating away
from their page. WebMCP therefore complements, rather than replaces, a server
MCP or API that must work without an open tab.

## Trust boundaries

1. Tool names, descriptions, annotations, arguments, outputs, and page content
   are untrusted. A `readOnlyHint` is metadata, not proof.
2. The browser performs a safety review for each call, but the application
   remains responsible for authentication, authorization, tenant isolation,
   validation, auditability, and correct effects.
3. Consequential actions such as sending messages, purchases, deletion,
   permissions, publishing, or personal-data sharing require confirmation at
   the moment of effect. Existing global production rules still apply.
4. Never accept a target account, tenant, role, or owner solely from tool
   input. Derive actor identity from the authenticated server session and
   authorize the requested object server-side.
5. Avoid generic proxy tools. An arbitrary URL fetcher, selector clicker,
   expression evaluator, file accessor, SQL runner, or free-form command turns
   a narrow page capability into an unbounded agent surface.
6. Return bounded structured data. Redact credentials, session artifacts,
   unrelated records, internal traces, and data outside the current task.

## Independent acceptance

Site tools test semantic correctness and reuse of application logic. They do
not prove layout, focus behavior, responsiveness, accessibility, animation,
visual hierarchy, browser compatibility, or the ordinary human workflow.
Keep the AI Lab VM/Chrome journey as a separate acceptance boundary.
