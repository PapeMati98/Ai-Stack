# Implementation contract

Register a tool only after the page's application services are ready. Keep the
human UI fully functional when WebMCP is unavailable.

```javascript
export async function registerSiteTools(app) {
  const registerTool = document.modelContext?.registerTool;
  if (typeof registerTool !== "function") return;

  await app.siteTools?.remove?.("get_current_report_summary");

  const registration = await registerTool.call(document.modelContext, {
    name: "get_current_report_summary",
    description: "Read the summary of the report currently open on this page.",
    inputSchema: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    execute: async () => {
      const report = await app.reports.getCurrentAuthorizedReport();
      return {
        reportId: report.id,
        title: report.title,
        status: report.status,
        lastUpdatedAt: report.lastUpdatedAt,
        asOf: new Date().toISOString(),
      };
    },
  });

  app.siteTools?.track?.("get_current_report_summary", registration);
}
```

The example is intentionally read-only. `remove` and `track` represent the
application's own page-scoped registration registry; adapt them to the runtime's
returned registration/unregister contract instead of assuming one exact API
shape. Dispose registered tools on route teardown and before replacing a tool
with the same stable name.

For an aggregate read, include units, currency when relevant, effective
filters/scope, an `asOf` timestamp, and a stable filter fingerprint so another
check can reproduce the number.

For a write tool:

- call the same domain command used by the ordinary UI;
- authorize on the server using the active session;
- validate a closed input schema and current resource version;
- define duplicate/idempotency behavior;
- for consequential effects, bind the application's preview/confirmation token
  to actor, tenant, exact action, resource ID/version, expiry, and one-time use;
- return the changed object ID, prior/new version, and verification handle;
- ensure the UI refreshes or otherwise shows the effect;
- log the action without logging secrets or unrestricted payloads.

## Required tests

1. Registration succeeds when the capability exists and is a no-op when absent.
2. Tool names are unique and stable across navigation/re-render; teardown and
   replacement leave one active definition per name.
3. Valid read returns only authorized fields.
4. Extra properties, invalid enums, oversized text, and malformed IDs fail.
5. Unauthorized role, wrong tenant, and inaccessible object fail server-side.
6. Duplicate write calls have a defined safe outcome.
7. A consequential write rejects expired, replayed, wrong-actor, wrong-tenant,
   wrong-resource, or stale-version confirmation evidence.
8. Backend failure becomes a bounded user-facing error without a stack trace.
9. The ordinary UI path still works and produces equivalent domain behavior.
10. Native discovery is proven in a fresh supported desktop task.
11. A separate visual/user-role UAT proves the human experience.
