import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import * as policy from '../src/policy.mjs';

const {
  redactSensitivePaths,
  redactTextUrls,
  redactUrl,
  resolveTarget,
  sanitizeLogText,
  validateContract,
} = policy;

const testDir = path.dirname(fileURLToPath(import.meta.url));
const skillRoot = path.resolve(testDir, '..', '..', '..');

function config(overrides = {}) {
  return {
    schema_version: 2,
    runner_version: '0.3.0',
    browser_executable: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    allow_network: false,
    allowed_origins: [],
    capture_console_text: false,
    max_timeout_seconds: 300,
    max_actions: 50,
    desktop_enabled: true,
    desktop_task_name: 'MateuszAI-RemoteComputerUseDesktop',
    desktop_user: 'EXAMPLE-VM\\Example-UAT',
    desktop_max_text_length: 2000,
    desktop_allowed_apps: [{
      id: 'notepad',
      executable: 'C:\\Windows\\System32\\notepad.exe',
      arguments: [],
      process_names: ['notepad'],
    }],
    ...overrides,
  };
}

function desktopContract(overrides = {}) {
  return {
    schema_version: 2,
    task_id: 'desktop-pilot',
    mode: 'desktop',
    headless: false,
    side_effects: 'test-only',
    capability: 'uat',
    timeout_seconds: 60,
    target: { kind: 'app', app_id: 'notepad' },
    actions: [
      { type: 'launch', app_id: 'notepad' },
      { type: 'text', value: 'canary' },
      { type: 'screenshot', name: 'typed' },
      { type: 'close_target' },
    ],
    ...overrides,
  };
}

function contract(overrides = {}) {
  return {
    schema_version: 1,
    task_id: 'test-pilot',
    mode: 'browser',
    headless: true,
    side_effects: 'none',
    timeout_seconds: 60,
    target: { kind: 'fixture', value: 'pilot.html' },
    actions: [{ type: 'assert_visible', selector: 'h1' }],
    ...overrides,
  };
}

function browserUatContract(overrides = {}) {
  return {
    schema_version: 3,
    task_id: 'example-project-uat',
    mode: 'browser',
    headless: true,
    side_effects: 'test-only',
    capability: 'origin-uat',
    timeout_seconds: 120,
    target: { kind: 'url', value: 'http://127.0.0.1:13102/login' },
    actions: [
      { type: 'click', selector: '#login-dev-superadmin' },
      {
        type: 'set_fixture_file',
        selector: 'input[type=file]',
        fixture_id: 'example-synthetic-2026-07',
        fixture_sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      },
      { type: 'assert_text', selector: '[role=status]', contains: 'Zaimportowano' },
    ],
    ...overrides,
  };
}

test('accepts the bounded offline contract', () => {
  assert.equal(validateContract(contract(), config()).task_id, 'test-pilot');
});

test('rejects unknown contract fields', () => {
  assert.throws(() => validateContract(contract({ command: 'whoami' }), config()), /unsupported field/);
});

test('rejects a network target while network is disabled', () => {
  assert.throws(
    () => validateContract(contract({ target: { kind: 'url', value: 'https://staging.example.test/' } }), config()),
    /network targets are disabled/,
  );
});

test('allows only an exact configured origin', () => {
  const networkConfig = config({ allow_network: true, allowed_origins: ['https://staging.example.test'] });
  validateContract(
    contract({ target: { kind: 'url', value: 'https://staging.example.test/path?secret=redacted' } }),
    networkConfig,
  );
  assert.throws(
    () => validateContract(contract({ target: { kind: 'url', value: 'https://other.example.test/' } }), networkConfig),
    /not allowlisted/,
  );
});

test('network browser tasks cannot perform input under side_effects none', () => {
  const networkConfig = config({ allow_network: true, allowed_origins: ['https://staging.example.test'] });
  assert.throws(
    () => validateContract(contract({
      target: { kind: 'url', value: 'https://staging.example.test/' },
      actions: [{ type: 'click', selector: '#submit' }],
    }), networkConfig),
    /cannot perform input actions/,
  );
});

test('accepts exact-origin browser UAT with a catalogued fixture id contract', () => {
  const networkConfig = config({
    allow_network: true,
    allowed_origins: ['http://127.0.0.1:13102'],
  });
  const validated = validateContract(browserUatContract(), networkConfig);
  assert.equal(validated.capability, 'origin-uat');
  assert.equal(validated.actions[1].fixture_id, 'example-synthetic-2026-07');
});

test('browser UAT allows bounded select option without enabling it for read-only network tasks', () => {
  const networkConfig = config({
    allow_network: true,
    allowed_origins: ['http://127.0.0.1:13102'],
  });
  const selectAction = {
    type: 'select_option',
    selector: 'select[aria-label="Okres rozliczeń"]',
    value: '2026-08-01_2026-08-27',
  };
  const validated = validateContract(
    browserUatContract({ actions: [selectAction] }),
    networkConfig,
  );
  assert.equal(validated.actions[0].type, 'select_option');
  assert.throws(
    () => validateContract(contract({ actions: [selectAction] }), config()),
    /unsupported for browser/,
  );
  assert.throws(
    () => validateContract(
      browserUatContract({ actions: [{ ...selectAction, value: 'x'.repeat(129) }] }),
      networkConfig,
    ),
    /value/,
  );
});

test('browser UAT privacy mode rejects text entry, key presses, and screenshots', () => {
  const networkConfig = config({
    allow_network: true,
    allowed_origins: ['http://127.0.0.1:13102'],
  });
  for (const action of [
    { type: 'fill', selector: '#secret', value: 'must-not-be-captured' },
    { type: 'press', selector: '#secret', key: 'Tab' },
    { type: 'screenshot', name: 'must-not-exist' },
  ]) {
    assert.throws(
      () => validateContract(browserUatContract({ actions: [action] }), networkConfig),
      /unsupported for browser-origin-uat/,
    );
  }
});

test('browser UAT fails closed for multiple origins, non-URL targets, paths, and unsafe fixture ids', () => {
  assert.throws(
    () => validateContract(
      browserUatContract(),
      config({
        allow_network: true,
        allowed_origins: ['http://127.0.0.1:13102', 'https://example.test'],
      }),
    ),
    /exactly one allowed origin/,
  );
  assert.throws(
    () => validateContract(
      browserUatContract({ target: { kind: 'fixture', value: 'pilot.html' } }),
      config({ allow_network: true, allowed_origins: ['http://127.0.0.1:13102'] }),
    ),
    /URL target/,
  );
  assert.throws(
    () => validateContract(
      browserUatContract({
        actions: [{
          type: 'set_fixture_file',
          selector: 'input[type=file]',
          fixture_id: 'example-synthetic-2026-07',
          fixture_sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          path: 'C:\\Users\\Example\\statement.txt',
        }],
      }),
      config({ allow_network: true, allowed_origins: ['http://127.0.0.1:13102'] }),
    ),
    /unsupported field/,
  );
  assert.throws(
    () => validateContract(
      browserUatContract({
        actions: [{
          type: 'set_fixture_file',
          selector: 'input[type=file]',
          fixture_id: '../statement',
          fixture_sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        }],
      }),
      config({ allow_network: true, allowed_origins: ['http://127.0.0.1:13102'] }),
    ),
    /fixture_id is invalid/,
  );
});

test('browser UAT network policy allows only exact-origin GET HEAD POST', () => {
  assert.equal(typeof policy.isNetworkRequestPermitted, 'function');
  const networkConfig = config({
    allow_network: true,
    allowed_origins: ['http://127.0.0.1:13102'],
  });
  const uat = browserUatContract();
  for (const method of ['GET', 'HEAD', 'POST']) {
    assert.equal(
      policy.isNetworkRequestPermitted(uat, networkConfig, method, 'http://127.0.0.1:13102/api/payments/upload-mt940'),
      true,
    );
  }
  for (const [method, url] of [
    ['PUT', 'http://127.0.0.1:13102/api/payments/upload-mt940'],
    ['DELETE', 'http://127.0.0.1:13102/api/payments/upload-mt940'],
    ['OPTIONS', 'http://127.0.0.1:13102/api/payments/upload-mt940'],
    ['POST', 'http://127.0.0.1:13103/api/payments/upload-mt940'],
    ['GET', 'http://127.0.0.1:13102.evil.test/'],
    ['GET', 'https://example.test/redirect'],
  ]) {
    assert.equal(policy.isNetworkRequestPermitted(uat, networkConfig, method, url), false);
  }
});

test('file fixture resolution is jail-bound and verifies catalog id size and sha256', () => {
  assert.equal(typeof policy.resolveFileFixture, 'function');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'rcu-file-fixture-'));
  const fixtureRoot = path.join(root, 'fixtures');
  const fileRoot = path.join(fixtureRoot, 'files');
  fs.mkdirSync(fileRoot, { recursive: true });
  const fileName = 'example-synthetic-2026-07.txt';
  const filePath = path.join(fileRoot, fileName);
  const bytes = Buffer.from('synthetic-only\n', 'utf8');
  fs.writeFileSync(filePath, bytes);
  const catalog = {
    schema_version: 1,
    fixtures: [{
      id: 'example-synthetic-2026-07',
      file_name: fileName,
      classification: 'synthetic',
      size_bytes: bytes.length,
      sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    }],
  };
  fs.writeFileSync(path.join(fixtureRoot, 'file-catalog.json'), JSON.stringify(catalog));

  const expectedSha = catalog.fixtures[0].sha256;
  assert.equal(
    policy.resolveFileFixture('example-synthetic-2026-07', expectedSha, root),
    fs.realpathSync(filePath),
  );
  assert.throws(() => policy.resolveFileFixture('unknown-fixture', expectedSha, root), /not catalogued/);
  assert.throws(
    () => policy.resolveFileFixture('example-synthetic-2026-07', 'b'.repeat(64), root),
    /contract SHA-256 does not match/,
  );
  fs.writeFileSync(filePath, 'drifted');
  assert.throws(
    () => policy.resolveFileFixture('example-synthetic-2026-07', expectedSha, root),
    /size or SHA-256 mismatch/,
  );

  catalog.fixtures[0].file_name = '../outside.txt';
  fs.writeFileSync(path.join(fixtureRoot, 'file-catalog.json'), JSON.stringify(catalog));
  assert.throws(
    () => policy.resolveFileFixture('example-synthetic-2026-07', expectedSha, root),
    /file_name is invalid/,
  );
  fs.rmSync(root, { recursive: true, force: true });
});

test('rejects URL credentials and unknown runner config fields', () => {
  const networkConfig = config({ allow_network: true, allowed_origins: ['https://staging.example.test'] });
  assert.throws(
    () => validateContract(
      contract({ target: { kind: 'url', value: 'https://user:secret@staging.example.test/' } }),
      networkConfig,
    ),
    /must not contain credentials/,
  );
  assert.throws(() => validateContract(contract(), config({ arbitrary_shell: true })), /unsupported field/);
});

test('rejects traversal fixture names and Enter key submission', () => {
  assert.throws(
    () => validateContract(contract({ target: { kind: 'fixture', value: '../pilot.html' } }), config()),
    /fixture name is invalid/,
  );
  assert.throws(
    () => validateContract(contract({ actions: [{ type: 'press', selector: 'input', key: 'Enter' }] }), config()),
    /not allowed/,
  );
});

test('resolves fixtures only under the installed fixture root', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'rcu-policy-'));
  fs.mkdirSync(path.join(root, 'fixtures'));
  fs.writeFileSync(path.join(root, 'fixtures', 'pilot.html'), '<h1>pilot</h1>');
  const value = resolveTarget(contract(), config(), root);
  assert.match(value, /^file:/);
  fs.rmSync(root, { recursive: true, force: true });
});

test('redacts query strings, fragments, and fixture paths', () => {
  assert.equal(redactUrl('https://example.test/path?token=secret#section'), 'https://example.test/path');
  assert.equal(redactUrl('file:///C:/private/pilot.html'), 'file://installed-fixture');
  assert.equal(
    redactTextUrls('Navigation failed at https://user:secret@example.test/path?token=secret#x'),
    'Navigation failed at https://example.test/path',
  );
});

test('redacts installed fixture realpaths from privacy-mode errors', () => {
  const fixtureRoot = 'C:\\ProgramData\\MateuszAI\\remote-computer-use\\current\\fixtures';
  const message = [
    `setInputFiles failed for ${fixtureRoot}\\files\\example-synthetic.txt`,
    'C:/ProgramData/MateuszAI/remote-computer-use/current/fixtures/files/example-synthetic.txt',
  ].join(' | ');

  const redacted = redactSensitivePaths(message, [fixtureRoot]);

  assert.equal(redacted.includes('C:\\ProgramData\\MateuszAI'), false);
  assert.equal(redacted.includes('C:/ProgramData/MateuszAI'), false);
  assert.equal(redacted.includes('[redacted-path]'), true);
});

test('sanitizes ANSI and control characters from runner error evidence', () => {
  const raw = '\u001b[2mlocator.click failed\u001b[22m\nretry\u0000';

  const sanitized = sanitizeLogText(raw);

  assert.equal(sanitized, 'locator.click failed\nretry');
});

test('accepts a bounded allowlisted desktop UAT contract', () => {
  assert.equal(validateContract(desktopContract(), config()).mode, 'desktop');
});

test('observe mode rejects input and requires side_effects none', () => {
  const observe = desktopContract({
    capability: 'observe',
    side_effects: 'none',
    target: { kind: 'desktop', app_id: 'desktop' },
    actions: [{ type: 'screenshot', name: 'desktop' }],
  });
  validateContract(observe, config());
  assert.throws(
    () => validateContract({ ...observe, actions: [{ type: 'text', value: 'no' }] }, config()),
    /may only wait or take screenshots/,
  );
});

test('desktop UAT rejects non-allowlisted launch and unsafe key', () => {
  assert.throws(
    () => validateContract(desktopContract({
      target: { kind: 'app', app_id: 'powershell' },
      actions: [{ type: 'launch', app_id: 'powershell' }],
    }), config()),
    /allowlisted/,
  );
  assert.throws(
    () => validateContract(desktopContract({
      actions: [
        { type: 'launch', app_id: 'notepad' },
        { type: 'key', key: 'F12', modifiers: [] },
      ],
    }), config()),
    /not allowed/,
  );
});

test('desktop mode fails closed when configuration disables it', () => {
  assert.throws(
    () => validateContract(desktopContract(), config({ desktop_enabled: false })),
    /disabled/,
  );
});

test('accepts the close-target-only Chrome canary', () => {
  const canary = JSON.parse(fs.readFileSync(path.join(skillRoot, 'examples', 'desktop-chrome-canary.json'), 'utf8'));
  const chromeConfig = config({
    desktop_allowed_apps: [{
      id: 'chrome-agent-uat',
      executable: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
      arguments: [],
      process_names: ['chrome'],
    }],
  });
  const validated = validateContract(canary, chromeConfig);
  assert.equal(validated.target.app_id, 'chrome-agent-uat');
  assert.deepEqual(validated.actions.map((action) => action.type), ['launch', 'wait', 'screenshot', 'close_target']);
});

test('Chrome graceful exit reasserts the target after the menu wait', () => {
  const broker = fs.readFileSync(path.join(skillRoot, 'assets', 'vm-runner', 'scripts', 'desktop-broker.ps1'), 'utf8');
  const start = broker.indexOf('function Request-GracefulTargetExit');
  const end = broker.indexOf('function Stop-StartedTarget', start);
  assert.ok(start >= 0 && end > start, 'graceful-exit function must exist');
  const block = broker.slice(start, end);
  const wait = block.indexOf('Start-Sleep -Milliseconds 500');
  const reassert = block.indexOf('Assert-TargetForeground $StartedProcess $AppPolicy', wait);
  const keyDown = block.indexOf('KeyDown([uint16]0x58)', wait);
  assert.ok(wait >= 0 && reassert > wait && keyDown > reassert, 'target must be reasserted immediately before the Exit key');
});

test('rejects unsafe desktop boundary values', () => {
  assert.throws(
    () => validateContract(desktopContract({
      actions: [
        { type: 'launch', app_id: 'notepad' },
        { type: 'text', value: 'x'.repeat(2001) },
        { type: 'close_target' },
      ],
    }), config()),
    /length 0\.\.2000/,
  );
  assert.throws(
    () => validateContract(desktopContract({
      actions: [
        { type: 'launch', app_id: 'notepad' },
        { type: 'click', x: 10, y: 10, button: 'middle', count: 1 },
        { type: 'close_target' },
      ],
    }), config()),
    /click shape is invalid/,
  );
  assert.throws(
    () => validateContract(desktopContract({
      actions: [
        { type: 'launch', app_id: 'notepad' },
        { type: 'close_target', force: true },
      ],
    }), config()),
    /unsupported field/,
  );
  assert.throws(
    () => validateContract(desktopContract(), config({ desktop_user: 'Example' })),
    /explicit domain\\user identity/,
  );
});
