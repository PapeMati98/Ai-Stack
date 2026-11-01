/* Trusted in-host seam. Python cannot invoke the Codex app tools.
 * The caller supplies an operator registry and the existing Office native-begin
 * fence. This file owns no queue, timers, bindings or production configuration.
 * A returned threadId is not a native message receipt or an application ACK.
 */
const CODEX_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function codexToolPayload(result) {
  if (!result || result.isError) return null;
  if (result.structuredContent && typeof result.structuredContent === 'object') {
    return result.structuredContent;
  }
  const texts = (result.content || []).filter(x => x.type === 'text');
  if (texts.length !== 1) return null;
  try { return JSON.parse(texts[0].text); } catch { return null; }
}

function codexSnapshot(result, target) {
  const data = codexToolPayload(result);
  const rows = data && Array.isArray(data.polls) ? data.polls.filter(x =>
    x.thread && x.thread.id === target.threadId && x.thread.hostId === target.hostId) : [];
  if (rows.length !== 1) return null;
  const row = rows[0];
  return {activity: row.thread.status && row.thread.status.type,
    turnId: row.latestTurn && row.latestTurn.id,
    turnStatus: row.latestTurn && row.latestTurn.status,
    startedAt: row.latestTurn && row.latestTurn.startedAt};
}

async function codexNativeSubmit(tools, intent, operator, nativeBegin) {
  // operator is host-owned configuration, never parsed from an Office body.
  const target = operator && operator.targets && operator.targets[intent && intent.target];
  if (!target || target.authorized !== true || !CODEX_UUID.test(target.threadId)
      || target.threadId !== intent.target || target.hostId !== 'local'
      || intent.driver !== 'codex-desktop-relay'
      || intent.relayAgent !== operator.relayAgent
      || intent.projectSlug !== target.projectSlug
      || intent.workspace !== target.workspace
      || typeof intent.prompt !== 'string' || !intent.prompt || intent.prompt.length > 8192
      || typeof intent.dispatchId !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(intent.dispatchId)
      || typeof nativeBegin !== 'function') {
    return {status: 'REFUSED', reason: 'untrusted_or_mismatched_intent', sendCalls: 0};
  }
  const before = codexSnapshot(await tools.mcp__codex_app__wait_threads({
    targets: [{threadId: target.threadId, hostId: target.hostId}], timeoutMs: 0}), target);
  if (!before || before.activity !== 'idle' || before.turnStatus === 'inProgress') {
    return {status: 'DEFERRED', reason: 'busy_or_unverified_target', sendCalls: 0};
  }
  let quota = 'unknown';
  if (typeof tools.mcp__codex_app__get_usage_limits === 'function') {
    try {
      const usage = codexToolPayload(await tools.mcp__codex_app__get_usage_limits({}));
      if (usage && (usage.ordinaryUsageAllowed === false
          || usage.rateLimits && (usage.rateLimits.spendControlReached === true
            || usage.rateLimits.rateLimitReachedType))) {
        return {status: 'DEFERRED', reason: 'confirmed_quota_or_spend_block', sendCalls: 0};
      }
      if (usage && usage.ordinaryUsageAllowed === true) quota = 'available';
    } catch { /* Unknown is never reported as exhausted or a full allowance. */ }
  }
  // The callback revalidates current STOP, binding, driver and claim actor on
  // the authoritative server and durably fences submission before any send.
  const permit = await nativeBegin(intent);
  if (!permit || permit.allowed !== true || permit.dispatchId !== intent.dispatchId
      || permit.driver !== intent.driver || permit.target !== intent.target
      || permit.relayAgent !== intent.relayAgent) {
    return {status: 'REFUSED', reason: 'native_begin_not_verified', sendCalls: 0};
  }
  let raw;
  try {
    raw = await tools.mcp__codex_app__send_message_to_thread({
      threadId: target.threadId, hostId: target.hostId, prompt: intent.prompt});
  } catch {
    return {status: 'DELIVERY_UNKNOWN', reason: 'official_tool_outcome_unknown', sendCalls: 1};
  }
  const payload = codexToolPayload(raw);
  if (!payload || payload.threadId !== target.threadId) {
    return {status: 'DELIVERY_UNKNOWN', reason: 'official_tool_receipt_mismatch', sendCalls: 1};
  }
  let after;
  try {
    after = codexSnapshot(await tools.mcp__codex_app__wait_threads({
      targets: [{threadId: target.threadId, hostId: target.hostId}], timeoutMs: 50000}), target);
  } catch { /* The durable fence remains; never resend after failed observation. */ }
  const newTurn = after && CODEX_UUID.test(after.turnId || '')
    && after.turnId !== before.turnId
    && ['inProgress', 'completed'].includes(after.turnStatus);
  return {status: 'DELIVERY_UNKNOWN', reason: 'thread_id_only_not_native_message_receipt',
    dispatchId: intent.dispatchId, threadId: target.threadId,
    beforeTurnId: before.turnId || null,
    actualToolPayload: {threadId: payload.threadId}, quota,
    observedNewTurn: newTurn ? {turnId: after.turnId, status: after.turnStatus,
      startedAt: after.startedAt} : null,
    taskReceived: false, // Only a correlated Office ACK can prove task reception.
    sendCalls: 1};
}

async function codexOfficeCycle(tools, office, operator, threadId, timeoutSeconds = 50) {
  // office(op,args) is the trusted remote client with the relay identity already
  // fixed by the host. No Office body controls it. A cycle awaits one existing
  // watcher; it creates neither a queue nor a scheduler and performs no retry.
  const target = operator && operator.targets && operator.targets[threadId];
  if (!target || target.authorized !== true || !CODEX_UUID.test(threadId)
      || target.threadId !== threadId || target.hostId !== 'local'
      || !/^[a-z0-9][a-z0-9._-]{0,127}$/.test(target.bindingId || '')
      || target.officeAgent !== `codex-ailab-${threadId}`
      || typeof office !== 'function' || !Number.isInteger(timeoutSeconds)
      || timeoutSeconds < 1 || timeoutSeconds > 50) {
    return {status: 'REFUSED', reason: 'untrusted_cycle_target', sendCalls: 0};
  }
  const driver = 'codex-desktop-relay';
  const watched = await office('wake-relay-watch', {binding_id:[target.bindingId],
    driver, timeout_s:timeoutSeconds, interval_s:1, delay_s:0});
  if (!watched || watched.status !== 'ok' || watched.code !== 'wake_relay_watch'
      || watched.result !== 'pending' || !Array.isArray(watched.binding_ids)
      || watched.binding_ids.length !== 1 || watched.binding_ids[0] !== target.bindingId) {
    return {status: 'DEFERRED', reason: 'no_confirmed_office_event', sendCalls: 0};
  }
  const pending = await office('wake-relay-pending', {binding_id:target.bindingId, driver});
  if (!pending || pending.status !== 'ok' || pending.code !== 'wake_relay_pending'
      || !Array.isArray(pending.dispatches) || pending.dispatches.length > 20) {
    return {status:'REFUSED', reason:'unverified_pending', sendCalls:0};
  }
  const rows = pending.dispatches.filter(x => x.binding_id === target.bindingId
    && x.status === 'pending');
  rows.sort((a,b) => String(a.created_at).localeCompare(String(b.created_at))
    || String(a.dispatch_id).localeCompare(String(b.dispatch_id)));
  const row = rows[0];
  if (!row || row.target_local_id !== threadId || !/^[a-f0-9]{32}$/.test(row.dispatch_id || '')
      || !/^[a-f0-9]{32}$/.test(row.trigger_ref || '') || typeof row.text !== 'string'
      || !row.text || row.text.length > 8192 || row.text.includes('\0')) {
    return {status:'REFUSED', reason:'unverified_dispatch', sendCalls:0};
  }
  const intent = {target:threadId, driver, relayAgent:operator.relayAgent,
    projectSlug:target.projectSlug, workspace:target.workspace,
    dispatchId:row.dispatch_id, prompt:row.text};
  const submitted = await codexNativeSubmit(tools, intent, operator, async i => {
    const claimed = await office('wake-relay-claim', {dispatch_id:i.dispatchId,
      relay_agent:i.relayAgent, driver});
    if (!claimed || claimed.status !== 'ok' || claimed.code !== 'wake_relay_claimed'
        || !claimed.dispatch || claimed.dispatch.dispatch_id !== i.dispatchId
        || claimed.dispatch.status !== 'claimed') return null;
    const begun = await office('wake-relay-native-begin', {dispatch_id:i.dispatchId,
      relay_agent:i.relayAgent, driver});
    if (!begun || begun.status !== 'ok' || begun.code !== 'wake_native_submission_fenced'
        || !begun.dispatch || begun.dispatch.dispatch_id !== i.dispatchId
        || begun.dispatch.status !== 'delivery_unknown') return null;
    return {allowed:true, dispatchId:i.dispatchId, driver, target:i.target,
      relayAgent:i.relayAgent};
  });
  const delivered = {...submitted, officeTrigger:row.trigger_ref,
    bindingId:target.bindingId};
  if (delivered.status === 'DEFERRED' && delivered.reason === 'busy_or_unverified_target') {
    await office('wake-relay-defer-busy', {dispatch_id:row.dispatch_id, driver});
  }
  if (delivered.sendCalls !== 1 || !delivered.actualToolPayload
      || !delivered.observedNewTurn) return delivered;
  return codexSettleEvidence(office, target, delivered);
}

async function codexSettleEvidence(office, target, delivered) {
  const driver = 'codex-desktop-relay';
  // A native turn alone does not establish which task it read. Only the exact
  // recipient's durable ACK for this Office trigger allows settlement.
  const ack = await office('wake-relay-ack', {dispatch_id:delivered.dispatchId, driver});
  const ackVerified = ack && ack.status === 'ok' && ack.code === 'wake_relay_ack'
    && ack.dispatch_id === delivered.dispatchId && ack.target === target.officeAgent
    && ack.trigger_ref === delivered.officeTrigger && ack.acknowledged === true
    && /^[a-f0-9]{32}$/.test(ack.ack_id || '');
  if (!ackVerified) {
    return {...delivered, reason:'correlated_office_ack_pending'};
  }
  const receipt = await office('wake-relay-receipt', {dispatch_id:delivered.dispatchId,
    driver, status:'delivered', turn_id:delivered.observedNewTurn.turnId,
    detail:'correlated_office_ack_and_new_native_turn'});
  if (!receipt || receipt.status !== 'ok' || receipt.code !== 'wake_relay_receipt') {
    return {...delivered, reason:'correlated_evidence_not_settled'};
  }
  const stage = delivered.observedNewTurn.status === 'completed' ? 'target_finished' : 'target_started';
  const observed = await office('wake-relay-observe', {dispatch_id:delivered.dispatchId, driver, stage});
  if (!observed || observed.status !== 'ok' || observed.code !== 'wake_relay_observed') {
    return {...delivered, reason:'stage_not_recorded', taskReceived:true};
  }
  return {...delivered, status:'DELIVERED', reason:'new_native_turn_and_correlated_office_ack',
    taskReceived:true, observedStage:stage, ackId:ack.ack_id};
}

async function codexOfficeReconcile(tools, office, operator, previous) {
  // previous is the host's retained outcome, not data parsed from Office text.
  // Reconciliation never claims, releases the fence, or invokes send.
  const target = operator && operator.targets && operator.targets[previous && previous.threadId];
  if (!target || target.authorized !== true || target.hostId !== 'local'
      || target.threadId !== previous.threadId || !CODEX_UUID.test(target.threadId)
      || target.officeAgent !== `codex-ailab-${target.threadId}`
      || target.bindingId !== previous.bindingId
      || previous.status !== 'DELIVERY_UNKNOWN' || previous.sendCalls !== 1
      || !/^[a-f0-9]{32}$/.test(previous.dispatchId || '')
      || !/^[a-f0-9]{32}$/.test(previous.officeTrigger || '')
      || !previous.actualToolPayload || previous.actualToolPayload.threadId !== target.threadId
      || !previous.observedNewTurn || !CODEX_UUID.test(previous.observedNewTurn.turnId || '')
      || previous.observedNewTurn.turnId === previous.beforeTurnId
      || typeof office !== 'function') {
    return {status:'REFUSED', reason:'unverified_reconciliation_record', sendCalls:0};
  }
  let snapshot;
  try {
    snapshot = codexSnapshot(await tools.mcp__codex_app__wait_threads({
      targets:[{threadId:target.threadId, hostId:target.hostId}], timeoutMs:0}), target);
  } catch { return {...previous, reason:'native_turn_not_reconciled'}; }
  if (!snapshot || snapshot.turnId !== previous.observedNewTurn.turnId
      || !['inProgress','completed'].includes(snapshot.turnStatus)) {
    return {...previous, reason:'native_turn_not_reconciled'};
  }
  return codexSettleEvidence(office, target, {...previous, observedNewTurn:{
    turnId:snapshot.turnId, status:snapshot.turnStatus, startedAt:snapshot.startedAt}});
}

if (typeof module !== 'undefined') module.exports = {codexToolPayload, codexSnapshot,
  codexNativeSubmit, codexOfficeCycle, codexOfficeReconcile};
