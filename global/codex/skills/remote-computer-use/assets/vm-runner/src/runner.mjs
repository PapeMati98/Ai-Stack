import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright-core';

import {
  isNetworkRequestPermitted,
  redactSensitivePaths,
  redactTextUrls,
  redactUrl,
  resolveFileFixture,
  resolveTarget,
  sanitizeLogText,
  validateConfig,
  validateContract,
} from './policy.mjs';

const RUNNER_VERSION = '0.3.0';

function readJson(filePath, label) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (error) {
    throw new Error(`${label} is not valid JSON: ${error.message}`);
  }
}

function writeJsonAtomic(filePath, value) {
  const temporary = `${filePath}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' });
  fs.renameSync(temporary, filePath);
}

function assertArtifactRoot(artifactDir) {
  const resolved = path.resolve(artifactDir);
  if (!path.isAbsolute(resolved) || resolved.length < 8) {
    throw new Error('artifact directory is invalid');
  }
  fs.mkdirSync(resolved, { recursive: true });
  return resolved;
}

function safeError(error, sensitivePaths = []) {
  const message = error instanceof Error ? error.message : String(error);
  const redacted = sanitizeLogText(
    redactTextUrls(redactSensitivePaths(message, sensitivePaths)),
  );
  return redacted.slice(0, 2000);
}

async function performAction(page, action, artifactDir, runnerRoot) {
  switch (action.type) {
    case 'assert_title_contains': {
      const title = await page.title();
      if (!title.includes(action.contains)) {
        throw new Error('page title did not contain the expected text');
      }
      return { type: action.type, status: 'PASS' };
    }
    case 'assert_text': {
      const locator = page.locator(action.selector);
      await locator.waitFor({ state: 'visible' });
      await page.waitForFunction(
        ({ selector, contains }) => document.querySelector(selector)?.innerText.includes(contains) === true,
        { selector: action.selector, contains: action.contains },
      );
      return { type: action.type, selector: action.selector, status: 'PASS' };
    }
    case 'assert_visible': {
      await page.locator(action.selector).waitFor({ state: 'visible' });
      return { type: action.type, selector: action.selector, status: 'PASS' };
    }
    case 'click':
      await page.locator(action.selector).click();
      return { type: action.type, selector: action.selector, status: 'PASS' };
    case 'select_option': {
      const select = page.locator(action.selector);
      const isSelect = await select.evaluate((element) => element instanceof HTMLSelectElement);
      if (!isSelect) {
        throw new Error('select_option requires a select target');
      }
      const selected = await select.selectOption(action.value);
      if (selected.length !== 1 || selected[0] !== action.value) {
        throw new Error('select_option did not select the requested existing option');
      }
      return {
        type: action.type,
        selector: action.selector,
        value_logged: false,
        status: 'PASS',
      };
    }
    case 'fill':
      if (await page.locator(action.selector).evaluate(
        (element) => element instanceof HTMLInputElement && element.type === 'file',
      )) {
        throw new Error('file inputs require a catalogued set_fixture_file action');
      }
      await page.locator(action.selector).fill(action.value);
      return { type: action.type, selector: action.selector, status: 'PASS', value_logged: false };
    case 'press':
      await page.locator(action.selector).press(action.key);
      return { type: action.type, selector: action.selector, key: action.key, status: 'PASS' };
    case 'set_fixture_file': {
      const input = page.locator(action.selector);
      const isFileInput = await input.evaluate(
        (element) => element instanceof HTMLInputElement && element.type === 'file',
      );
      if (!isFileInput) {
        throw new Error('set_fixture_file requires an input[type=file] target');
      }
      const fixturePath = resolveFileFixture(
        action.fixture_id,
        action.fixture_sha256,
        runnerRoot,
      );
      try {
        await input.setInputFiles(fixturePath);
      } catch {
        throw new Error('catalogued fixture upload failed');
      }
      return {
        type: action.type,
        selector: action.selector,
        fixture_id: action.fixture_id,
        fixture_verified: true,
        status: 'PASS',
      };
    }
    case 'screenshot': {
      const screenshotPath = path.join(artifactDir, `${action.name}.png`);
      await page.screenshot({ path: screenshotPath, fullPage: false });
      return { type: action.type, name: action.name, status: 'PASS' };
    }
    default:
      throw new Error(`unsupported action: ${action.type}`);
  }
}

async function main() {
  const [contractPath, configPath, artifactArgument] = process.argv.slice(2);
  if (!contractPath || !configPath || !artifactArgument) {
    throw new Error('usage: runner.mjs <contract.json> <config.json> <artifact-dir>');
  }

  const artifactDir = assertArtifactRoot(artifactArgument);
  const runnerRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const config = validateConfig(readJson(configPath, 'runner config'));
  const contract = validateContract(readJson(contractPath, 'task contract'), config);
  const privacyMode = contract.schema_version === 3 && contract.capability === 'origin-uat';
  const target = resolveTarget(contract, config, runnerRoot);
  const startedAt = new Date();
  const consoleEvents = [];
  const networkEvents = [];
  const actionResults = [];
  let browser;
  let context;
  let page;
  let status = 'PASS';
  let errorMessage = null;
  let browserVersion = null;
  let tracingStarted = false;

  try {
    browser = await chromium.launch({
      executablePath: config.browser_executable,
      headless: true,
      args: [
        '--disable-background-networking',
        '--disable-component-update',
        '--disable-default-apps',
        '--disable-domain-reliability',
        '--disable-extensions',
        '--disable-sync',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
        '--metrics-recording-only',
        '--no-default-browser-check',
        '--no-first-run',
      ],
    });
    browserVersion = browser.version();
    context = await browser.newContext({
      acceptDownloads: false,
      offline: !config.allow_network,
      serviceWorkers: 'block',
      viewport: { width: 1440, height: 900 },
    });
    if (!config.allow_network) {
      await context.setOffline(true);
    }
    await context.routeWebSocket('**/*', async (webSocket) => {
      if (networkEvents.length < 1000) {
        networkEvents.push({ method: 'WEBSOCKET', url: redactUrl(webSocket.url()), decision: 'blocked' });
      }
      await webSocket.close({ code: 1008, reason: 'blocked by runner policy' });
    });
    await context.addInitScript(() => {
      const blockedTransport = (name) => class {
        constructor() {
          throw new DOMException(`${name} is blocked by runner policy`, 'SecurityError');
        }
      };
      for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'WebTransport']) {
        Object.defineProperty(globalThis, name, {
          configurable: false,
          enumerable: false,
          writable: false,
          value: blockedTransport(name),
        });
      }
    });
    if (!privacyMode) {
      await context.tracing.start({ screenshots: true, snapshots: true, sources: false });
      tracingStarted = true;
    }
    const fixturePrefix = pathToFileURL(`${path.resolve(runnerRoot, 'fixtures')}${path.sep}`).href;
    await context.route('**/*', async (route) => {
      const request = route.request();
      const method = request.method().toUpperCase();
      let parsed;
      try {
        parsed = new URL(request.url());
      } catch {
        networkEvents.push({ method, url: 'invalid-url', decision: 'blocked' });
        await route.abort('blockedbyclient');
        return;
      }

      const installedFixture = parsed.protocol === 'file:' && request.url().startsWith(fixturePrefix);
      const localProtocol = installedFixture || ['data:', 'about:', 'blob:'].includes(parsed.protocol);
      const allowedOrigin = config.allow_network && config.allowed_origins.includes(parsed.origin);
      const permitted = localProtocol || (
        allowedOrigin
        && isNetworkRequestPermitted(contract, config, method, request.url())
      );
      if (networkEvents.length < 1000) {
        networkEvents.push({ method, url: redactUrl(request.url()), decision: permitted ? 'allowed' : 'blocked' });
      }
      if (permitted) {
        await route.continue();
      } else {
        await route.abort('blockedbyclient');
      }
    });

    page = await context.newPage();
    page.setDefaultTimeout(Math.min(contract.timeout_seconds * 1000, 30000));
    page.on('console', (message) => {
      if (consoleEvents.length < 500) {
        const event = { type: message.type() };
        if (config.capture_console_text) {
          event.text = message.text().slice(0, 500);
        }
        consoleEvents.push(event);
      }
    });
    page.on('pageerror', (error) => {
      if (consoleEvents.length < 500) {
        const event = { type: 'pageerror' };
        if (config.capture_console_text) {
          event.text = safeError(error);
        }
        consoleEvents.push(event);
      }
    });
    page.on('dialog', async (dialog) => dialog.dismiss());
    page.on('download', async (download) => download.cancel());
    page.on('popup', async (popup) => popup.close());

    await page.goto(target, { waitUntil: 'domcontentloaded', timeout: contract.timeout_seconds * 1000 });
    for (const action of contract.actions) {
      actionResults.push(await performAction(page, action, artifactDir, runnerRoot));
    }
    if (!privacyMode) {
      await page.screenshot({ path: path.join(artifactDir, 'final.png'), fullPage: false });
    }
  } catch (error) {
    status = 'FAIL';
    errorMessage = safeError(error, privacyMode ? [runnerRoot, artifactDir] : []);
    if (page && !privacyMode) {
      try {
        await page.screenshot({ path: path.join(artifactDir, 'failure.png'), fullPage: false });
      } catch {
        // Preserve the original failure.
      }
    }
  } finally {
    if (context && tracingStarted) {
      try {
        await context.tracing.stop({ path: path.join(artifactDir, 'trace.zip') });
      } catch {
        // A missing trace must not hide the task verdict.
      }
    }
    if (browser) {
      await browser.close().catch(() => {});
    }
  }

  const finishedAt = new Date();
  fs.writeFileSync(path.join(artifactDir, 'console.json'), `${JSON.stringify(consoleEvents, null, 2)}\n`, 'utf8');
  fs.writeFileSync(path.join(artifactDir, 'network.json'), `${JSON.stringify(networkEvents, null, 2)}\n`, 'utf8');
  const result = {
    schema_version: 1,
    runner_version: RUNNER_VERSION,
    task_id: contract.task_id,
    mode: contract.mode,
    status,
    error: errorMessage,
    started_at: startedAt.toISOString(),
    finished_at: finishedAt.toISOString(),
    duration_seconds: Number(((finishedAt - startedAt) / 1000).toFixed(3)),
    target: redactUrl(target),
    browser_version: browserVersion,
    side_effects: contract.side_effects,
    privacy_mode: privacyMode ? 'synthetic-no-visual-capture' : 'standard',
    network_policy: (
      !config.allow_network
        ? 'offline-only'
        : contract.schema_version === 3
          ? 'exact-origin-get-head-post'
          : 'exact-origin-get-head'
    ),
    actions: actionResults,
    artifact_dir: artifactDir,
  };
  writeJsonAtomic(path.join(artifactDir, 'result.json'), result);
  process.stdout.write(`${JSON.stringify(result)}\n`);
  process.exitCode = status === 'PASS' ? 0 : 1;
}

main().catch((error) => {
  process.stderr.write(`${safeError(error)}\n`);
  process.exitCode = 2;
});
