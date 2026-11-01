import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const CONFIG_KEYS = new Set([
  'schema_version',
  'runner_version',
  'browser_executable',
  'allow_network',
  'allowed_origins',
  'capture_console_text',
  'max_timeout_seconds',
  'max_actions',
  'desktop_enabled',
  'desktop_task_name',
  'desktop_user',
  'desktop_max_text_length',
  'desktop_allowed_apps',
]);
const BROWSER_READ_CONTRACT_KEYS = new Set([
  'schema_version', 'task_id', 'mode', 'headless', 'side_effects',
  'timeout_seconds', 'target', 'actions',
]);
const BROWSER_UAT_CONTRACT_KEYS = new Set([
  'schema_version', 'task_id', 'mode', 'headless', 'side_effects',
  'capability', 'timeout_seconds', 'target', 'actions',
]);
const DESKTOP_CONTRACT_KEYS = new Set([
  'schema_version', 'task_id', 'mode', 'headless', 'side_effects',
  'capability', 'timeout_seconds', 'target', 'actions',
]);
const BROWSER_READ_ACTION_FIELDS = {
  assert_title_contains: new Set(['type', 'contains']),
  assert_text: new Set(['type', 'selector', 'contains']),
  assert_visible: new Set(['type', 'selector']),
  click: new Set(['type', 'selector']),
  fill: new Set(['type', 'selector', 'value']),
  press: new Set(['type', 'selector', 'key']),
  screenshot: new Set(['type', 'name']),
};
const BROWSER_UAT_ACTION_FIELDS = {
  assert_title_contains: new Set(['type', 'contains']),
  assert_text: new Set(['type', 'selector', 'contains']),
  assert_visible: new Set(['type', 'selector']),
  click: new Set(['type', 'selector']),
  select_option: new Set(['type', 'selector', 'value']),
  set_fixture_file: new Set(['type', 'selector', 'fixture_id', 'fixture_sha256']),
};
const DESKTOP_ACTION_FIELDS = {
  screenshot: new Set(['type', 'name']),
  wait: new Set(['type', 'milliseconds']),
  launch: new Set(['type', 'app_id']),
  move: new Set(['type', 'x', 'y']),
  click: new Set(['type', 'x', 'y', 'button', 'count']),
  text: new Set(['type', 'value']),
  key: new Set(['type', 'key', 'modifiers']),
  close_target: new Set(['type']),
};
const SAFE_BROWSER_KEYS = new Set([
  'Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
]);
const SAFE_DESKTOP_KEYS = new Set([
  'TAB', 'ENTER', 'ESCAPE', 'BACKSPACE', 'DELETE', 'SPACE', 'HOME', 'END',
  'PAGEUP', 'PAGEDOWN', 'ARROWUP', 'ARROWDOWN', 'ARROWLEFT', 'ARROWRIGHT', 'F5',
]);
const SAFE_MODIFIERS = new Set(['CTRL', 'SHIFT', 'ALT']);
const TASK_ID_RE = /^[a-z0-9][a-z0-9-]{2,63}$/;
const APP_ID_RE = /^[a-z0-9][a-z0-9-]{1,31}$/;
const PROCESS_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const WINDOWS_USER_RE = /^[A-Za-z0-9._-]{1,64}\\[A-Za-z0-9._-]{1,64}$/;
const FIXTURE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.html$/;
const FILE_FIXTURE_ID_RE = /^[a-z0-9][a-z0-9-]{2,63}$/;
const FILE_FIXTURE_NAME_RE = /^[a-z0-9][a-z0-9._-]{0,127}\.txt$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const SCREENSHOT_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const WINDOWS_ABSOLUTE_PATH_RE = /^[A-Za-z]:\\/;
const READ_ONLY_NETWORK_METHODS = new Set(['GET', 'HEAD']);
const ORIGIN_UAT_NETWORK_METHODS = new Set(['GET', 'HEAD', 'POST']);

function fail(message) {
  throw new Error(`contract-policy: ${message}`);
}

function assertObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    fail(`${label} must be an object`);
  }
}

function assertExactKeys(value, allowed, label) {
  const actual = new Set(Object.keys(value));
  for (const key of actual) {
    if (!allowed.has(key)) {
      fail(`${label} contains unsupported field: ${key}`);
    }
  }
  for (const key of allowed) {
    if (!actual.has(key)) {
      fail(`${label} is missing required field: ${key}`);
    }
  }
}

function assertString(value, label, minLength, maxLength) {
  if (typeof value !== 'string' || value.length < minLength || value.length > maxLength) {
    fail(`${label} must be a string of length ${minLength}..${maxLength}`);
  }
}

function validateAllowedApps(apps) {
  if (!Array.isArray(apps) || apps.length < 1 || apps.length > 16) {
    fail('config.desktop_allowed_apps must contain 1..16 apps');
  }
  const ids = new Set();
  for (const [index, app] of apps.entries()) {
    const label = `config.desktop_allowed_apps[${index}]`;
    assertObject(app, label);
    assertExactKeys(app, new Set(['id', 'executable', 'arguments', 'process_names']), label);
    if (typeof app.id !== 'string' || !APP_ID_RE.test(app.id) || ids.has(app.id)) {
      fail(`${label}.id is invalid or duplicated`);
    }
    ids.add(app.id);
    assertString(app.executable, `${label}.executable`, 3, 1024);
    if (!WINDOWS_ABSOLUTE_PATH_RE.test(app.executable)) {
      fail(`${label}.executable must be an absolute Windows path`);
    }
    if (!Array.isArray(app.arguments) || app.arguments.length > 16) {
      fail(`${label}.arguments must contain at most 16 static arguments`);
    }
    for (const argument of app.arguments) {
      assertString(argument, `${label}.argument`, 0, 512);
    }
    if (!Array.isArray(app.process_names) || app.process_names.length < 1 || app.process_names.length > 8) {
      fail(`${label}.process_names must contain 1..8 names`);
    }
    for (const processName of app.process_names) {
      if (typeof processName !== 'string' || !PROCESS_NAME_RE.test(processName)) {
        fail(`${label}.process_names contains an invalid name`);
      }
    }
  }
}

export function validateConfig(config) {
  assertObject(config, 'config');
  assertExactKeys(config, CONFIG_KEYS, 'config');
  if (config.schema_version !== 2 || config.runner_version !== '0.3.0') {
    fail('unsupported runner configuration');
  }
  assertString(config.browser_executable, 'config.browser_executable', 3, 1024);
  if (typeof config.allow_network !== 'boolean') {
    fail('config.allow_network must be boolean');
  }
  if (!Array.isArray(config.allowed_origins) || config.allowed_origins.length > 64) {
    fail('config.allowed_origins must contain at most 64 origins');
  }
  for (const origin of config.allowed_origins) {
    assertString(origin, 'allowed origin', 8, 512);
    const parsed = new URL(origin);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.origin !== origin) {
      fail(`allowed origin must be an exact HTTP(S) origin: ${origin}`);
    }
  }
  if (typeof config.capture_console_text !== 'boolean') {
    fail('config.capture_console_text must be boolean');
  }
  if (!Number.isInteger(config.max_timeout_seconds) || config.max_timeout_seconds < 10 || config.max_timeout_seconds > 900) {
    fail('config.max_timeout_seconds must be 10..900');
  }
  if (!Number.isInteger(config.max_actions) || config.max_actions < 1 || config.max_actions > 100) {
    fail('config.max_actions must be 1..100');
  }
  if (typeof config.desktop_enabled !== 'boolean') {
    fail('config.desktop_enabled must be boolean');
  }
  if (config.desktop_task_name !== 'MateuszAI-RemoteComputerUseDesktop') {
    fail('config.desktop_task_name is not the managed task name');
  }
  if (typeof config.desktop_user !== 'string' || !WINDOWS_USER_RE.test(config.desktop_user)) {
    fail('config.desktop_user must be an explicit domain\\user identity');
  }
  if (!Number.isInteger(config.desktop_max_text_length) || config.desktop_max_text_length < 1 || config.desktop_max_text_length > 4000) {
    fail('config.desktop_max_text_length must be 1..4000');
  }
  validateAllowedApps(config.desktop_allowed_apps);
  return config;
}

function validateCommonContract(contract, config) {
  if (typeof contract.task_id !== 'string' || !TASK_ID_RE.test(contract.task_id)) {
    fail('task_id is invalid');
  }
  if (
    !Number.isInteger(contract.timeout_seconds) ||
    contract.timeout_seconds < 10 ||
    contract.timeout_seconds > config.max_timeout_seconds
  ) {
    fail(`timeout_seconds must be 10..${config.max_timeout_seconds}`);
  }
}

function validateBrowserContract(contract, config) {
  const readOnly = contract.schema_version === 1;
  const originUat = contract.schema_version === 3;
  if (!readOnly && !originUat) {
    fail('browser tasks require schema version 1 or 3');
  }
  assertExactKeys(
    contract,
    originUat ? BROWSER_UAT_CONTRACT_KEYS : BROWSER_READ_CONTRACT_KEYS,
    'contract',
  );
  if (contract.mode !== 'browser' || contract.headless !== true) {
    fail('browser tasks require mode=browser and headless=true');
  }
  if (readOnly && contract.side_effects !== 'none') {
    fail('browser schema version 1 requires side_effects=none');
  }
  if (
    originUat
    && (contract.side_effects !== 'test-only' || contract.capability !== 'origin-uat')
  ) {
    fail('browser schema version 3 requires capability=origin-uat and side_effects=test-only');
  }
  assertObject(contract.target, 'target');
  assertExactKeys(contract.target, new Set(['kind', 'value']), 'target');
  assertString(contract.target.value, 'target.value', 1, 2048);
  if (originUat && contract.target.kind !== 'url') {
    fail('browser origin UAT requires a URL target');
  }
  if (contract.target.kind === 'fixture') {
    if (!FIXTURE_RE.test(contract.target.value)) {
      fail('fixture name is invalid');
    }
  } else if (contract.target.kind === 'url') {
    if (!config.allow_network) {
      fail('network targets are disabled');
    }
    const parsed = new URL(contract.target.value);
    if (!['http:', 'https:'].includes(parsed.protocol)) {
      fail('target URL must use HTTP or HTTPS');
    }
    if (parsed.username || parsed.password) {
      fail('target URL must not contain credentials');
    }
    if (!config.allowed_origins.includes(parsed.origin)) {
      fail(`target origin is not allowlisted: ${parsed.origin}`);
    }
    if (
      originUat
      && (
        config.allowed_origins.length !== 1
        || parsed.origin !== config.allowed_origins[0]
      )
    ) {
      fail('browser origin UAT requires exactly one allowed origin matching the target');
    }
  } else {
    fail('target.kind must be fixture or url');
  }
  validateActions(
    contract.actions,
    config,
    originUat ? BROWSER_UAT_ACTION_FIELDS : BROWSER_READ_ACTION_FIELDS,
    originUat ? 'browser-origin-uat' : 'browser',
  );
  if (
    readOnly
    && contract.target.kind === 'url'
    && contract.actions.some((action) => ['click', 'fill', 'press'].includes(action.type))
  ) {
    fail('network browser tasks with side_effects=none cannot perform input actions');
  }
}

function validateDesktopContract(contract, config) {
  assertExactKeys(contract, DESKTOP_CONTRACT_KEYS, 'contract');
  if (contract.schema_version !== 2 || contract.mode !== 'desktop') {
    fail('desktop tasks require schema version 2');
  }
  if (!config.desktop_enabled) {
    fail('desktop tasks are disabled by runner configuration');
  }
  if (contract.headless !== false) {
    fail('desktop tasks require headless=false');
  }
  if (!['observe', 'uat'].includes(contract.capability)) {
    fail('desktop capability must be observe or uat');
  }
  if (
    (contract.capability === 'observe' && contract.side_effects !== 'none') ||
    (contract.capability === 'uat' && contract.side_effects !== 'test-only')
  ) {
    fail('desktop side_effects does not match the requested capability');
  }
  assertObject(contract.target, 'target');
  assertExactKeys(contract.target, new Set(['kind', 'app_id']), 'target');
  if (contract.capability === 'observe') {
    if (contract.target.kind !== 'desktop' || contract.target.app_id !== 'desktop') {
      fail('observe tasks must target the desktop');
    }
  } else if (contract.target.kind !== 'app' || !findAllowedApp(config, contract.target.app_id)) {
    fail('uat tasks must target an allowlisted app');
  }
  validateActions(contract.actions, config, DESKTOP_ACTION_FIELDS, 'desktop');
  if (contract.capability === 'observe') {
    for (const action of contract.actions) {
      if (!['screenshot', 'wait'].includes(action.type)) {
        fail('observe tasks may only wait or take screenshots');
      }
    }
  } else {
    const launchActions = contract.actions.filter((action) => action.type === 'launch');
    if (launchActions.length !== 1 || launchActions[0].app_id !== contract.target.app_id) {
      fail('uat tasks require exactly one launch action for the target app');
    }
  }
}

function validateActions(actions, config, fieldsByType, mode) {
  if (!Array.isArray(actions) || actions.length < 1 || actions.length > config.max_actions) {
    fail(`actions must contain 1..${config.max_actions} items`);
  }
  actions.forEach((action, index) => {
    const label = `actions[${index}]`;
    assertObject(action, label);
    const allowed = fieldsByType[action.type];
    if (!allowed) {
      fail(`${label}.type is unsupported for ${mode}`);
    }
    assertExactKeys(action, allowed, label);
    if (mode.startsWith('browser')) {
      if (allowed.has('selector')) {
        assertString(action.selector, `${label}.selector`, 1, 512);
      }
      if (allowed.has('contains')) {
        assertString(action.contains, `${label}.contains`, 1, 1000);
      }
      if (action.type === 'fill') {
        assertString(action.value, `${label}.value`, 0, 1000);
      }
      if (action.type === 'select_option') {
        assertString(action.value, `${label}.value`, 1, 128);
      }
      if (action.type === 'press' && !SAFE_BROWSER_KEYS.has(action.key)) {
        fail(`${label}.key is not allowed`);
      }
      if (action.type === 'set_fixture_file') {
        if (
          typeof action.fixture_id !== 'string'
          || !FILE_FIXTURE_ID_RE.test(action.fixture_id)
        ) {
          fail(`${label}.fixture_id is invalid`);
        }
        if (
          typeof action.fixture_sha256 !== 'string'
          || !SHA256_RE.test(action.fixture_sha256)
        ) {
          fail(`${label}.fixture_sha256 is invalid`);
        }
      }
    } else {
      if (['move', 'click'].includes(action.type)) {
        if (!Number.isInteger(action.x) || !Number.isInteger(action.y) || action.x < -16384 || action.x > 16384 || action.y < -16384 || action.y > 16384) {
          fail(`${label} coordinates are invalid`);
        }
      }
      if (action.type === 'click' && (!['left', 'right'].includes(action.button) || ![1, 2].includes(action.count))) {
        fail(`${label} click shape is invalid`);
      }
      if (action.type === 'wait' && (!Number.isInteger(action.milliseconds) || action.milliseconds < 0 || action.milliseconds > 5000)) {
        fail(`${label}.milliseconds must be 0..5000`);
      }
      if (action.type === 'launch' && !findAllowedApp(config, action.app_id)) {
        fail(`${label}.app_id is not allowlisted`);
      }
      if (action.type === 'text') {
        assertString(action.value, `${label}.value`, 0, config.desktop_max_text_length);
      }
      if (action.type === 'key') {
        if (!SAFE_DESKTOP_KEYS.has(action.key)) {
          fail(`${label}.key is not allowed`);
        }
        if (!Array.isArray(action.modifiers) || action.modifiers.length > 3 || new Set(action.modifiers).size !== action.modifiers.length) {
          fail(`${label}.modifiers is invalid`);
        }
        for (const modifier of action.modifiers) {
          if (!SAFE_MODIFIERS.has(modifier)) {
            fail(`${label}.modifier is not allowed`);
          }
        }
      }
    }
    if (action.type === 'screenshot' && (typeof action.name !== 'string' || !SCREENSHOT_RE.test(action.name))) {
      fail(`${label}.name is invalid`);
    }
  });
}

export function findAllowedApp(config, appId) {
  return config.desktop_allowed_apps.find((app) => app.id === appId) ?? null;
}

export function validateContract(contract, config) {
  validateConfig(config);
  assertObject(contract, 'contract');
  validateCommonContract(contract, config);
  if (contract.mode === 'browser') {
    validateBrowserContract(contract, config);
  } else if (contract.mode === 'desktop') {
    validateDesktopContract(contract, config);
  } else {
    fail('mode must be browser or desktop');
  }
  return contract;
}

export function resolveTarget(contract, config, runnerRoot) {
  validateContract(contract, config);
  if (contract.mode !== 'browser') {
    fail('resolveTarget is only valid for browser contracts');
  }
  if (contract.target.kind === 'url') {
    return contract.target.value;
  }
  const fixtureRoot = path.resolve(runnerRoot, 'fixtures');
  const fixturePath = path.resolve(fixtureRoot, contract.target.value);
  if (!fixturePath.startsWith(`${fixtureRoot}${path.sep}`) || !fs.existsSync(fixturePath)) {
    fail('fixture does not exist under the runner fixture root');
  }
  const resolvedFixtureRoot = fs.realpathSync(fixtureRoot);
  const realFixturePath = fs.realpathSync(fixturePath);
  if (!realFixturePath.startsWith(`${resolvedFixtureRoot}${path.sep}`)) {
    fail('fixture escaped the runner fixture jail');
  }
  return pathToFileURL(realFixturePath).href;
}

export function isNetworkRequestPermitted(contract, config, method, value) {
  validateContract(contract, config);
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (!config.allow_network || !config.allowed_origins.includes(parsed.origin)) {
    return false;
  }
  const normalizedMethod = String(method).toUpperCase();
  if (contract.schema_version === 3 && contract.capability === 'origin-uat') {
    return (
      config.allowed_origins.length === 1
      && parsed.origin === new URL(contract.target.value).origin
      && ORIGIN_UAT_NETWORK_METHODS.has(normalizedMethod)
    );
  }
  return READ_ONLY_NETWORK_METHODS.has(normalizedMethod);
}

export function resolveFileFixture(fixtureId, expectedSha256, runnerRoot) {
  if (typeof fixtureId !== 'string' || !FILE_FIXTURE_ID_RE.test(fixtureId)) {
    fail('fixture_id is invalid');
  }
  if (typeof expectedSha256 !== 'string' || !SHA256_RE.test(expectedSha256)) {
    fail('fixture_sha256 is invalid');
  }
  const fixtureRoot = path.resolve(runnerRoot, 'fixtures');
  const fileRoot = path.resolve(fixtureRoot, 'files');
  const catalogPath = path.resolve(fixtureRoot, 'file-catalog.json');
  if (!fs.existsSync(catalogPath) || !fs.existsSync(fileRoot)) {
    fail('file fixture catalog is unavailable');
  }
  let catalog;
  try {
    catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8'));
  } catch {
    fail('file fixture catalog is invalid');
  }
  assertObject(catalog, 'file fixture catalog');
  assertExactKeys(catalog, new Set(['schema_version', 'fixtures']), 'file fixture catalog');
  if (
    catalog.schema_version !== 1
    || !Array.isArray(catalog.fixtures)
    || catalog.fixtures.length < 1
    || catalog.fixtures.length > 32
  ) {
    fail('file fixture catalog must contain 1..32 schema version 1 fixtures');
  }
  const ids = new Set();
  let selected = null;
  for (const [index, entry] of catalog.fixtures.entries()) {
    const label = `file fixture catalog.fixtures[${index}]`;
    assertObject(entry, label);
    assertExactKeys(
      entry,
      new Set(['id', 'file_name', 'classification', 'size_bytes', 'sha256']),
      label,
    );
    if (
      typeof entry.id !== 'string'
      || !FILE_FIXTURE_ID_RE.test(entry.id)
      || ids.has(entry.id)
    ) {
      fail(`${label}.id is invalid or duplicated`);
    }
    ids.add(entry.id);
    if (typeof entry.file_name !== 'string' || !FILE_FIXTURE_NAME_RE.test(entry.file_name)) {
      fail(`${label}.file_name is invalid`);
    }
    if (entry.classification !== 'synthetic') {
      fail(`${label}.classification must be synthetic`);
    }
    if (!Number.isInteger(entry.size_bytes) || entry.size_bytes < 1 || entry.size_bytes > 1048576) {
      fail(`${label}.size_bytes must be 1..1048576`);
    }
    if (typeof entry.sha256 !== 'string' || !SHA256_RE.test(entry.sha256)) {
      fail(`${label}.sha256 is invalid`);
    }
    if (entry.id === fixtureId) {
      selected = entry;
    }
  }
  if (!selected) {
    fail('fixture_id is not catalogued');
  }
  if (selected.sha256 !== expectedSha256) {
    fail('contract SHA-256 does not match the catalog');
  }
  const resolvedFileRoot = fs.realpathSync(fileRoot);
  const candidate = path.resolve(fileRoot, selected.file_name);
  if (!fs.existsSync(candidate)) {
    fail('catalogued fixture file is missing');
  }
  const realCandidate = fs.realpathSync(candidate);
  if (!realCandidate.startsWith(`${resolvedFileRoot}${path.sep}`)) {
    fail('catalogued fixture escaped the file fixture jail');
  }
  const stat = fs.statSync(realCandidate);
  const actualSha256 = crypto
    .createHash('sha256')
    .update(fs.readFileSync(realCandidate))
    .digest('hex');
  if (!stat.isFile() || stat.size !== selected.size_bytes || actualSha256 !== selected.sha256) {
    fail('catalogued fixture size or SHA-256 mismatch');
  }
  return realCandidate;
}

export function redactUrl(value) {
  try {
    const parsed = new URL(value);
    if (parsed.protocol === 'file:') {
      return 'file://installed-fixture';
    }
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return 'invalid-url';
  }
}

export function redactTextUrls(value) {
  return String(value).replace(/https?:\/\/[^\s\"'<>]+/gi, (url) => redactUrl(url));
}

export function redactSensitivePaths(value, sensitivePaths = []) {
  let redacted = String(value);
  for (const sensitivePath of sensitivePaths) {
    if (typeof sensitivePath !== 'string' || sensitivePath.length < 3) {
      continue;
    }
    const resolved = path.resolve(sensitivePath);
    const variants = new Set([
      resolved,
      resolved.replaceAll('\\', '/'),
      pathToFileURL(resolved).href,
    ]);
    for (const variant of variants) {
      redacted = redacted.replaceAll(variant, '[redacted-path]');
    }
  }
  return redacted;
}

export function sanitizeLogText(value) {
  return String(value)
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '');
}
