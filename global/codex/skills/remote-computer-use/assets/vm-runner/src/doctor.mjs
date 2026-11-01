import fs from 'node:fs';
import process from 'node:process';

import { validateConfig } from './policy.mjs';

const [configPath] = process.argv.slice(2);

try {
  if (!configPath) {
    throw new Error('config path is required');
  }
  const config = validateConfig(JSON.parse(fs.readFileSync(configPath, 'utf8')));
  if (!fs.existsSync(config.browser_executable)) {
    throw new Error('configured browser does not exist');
  }
  const missingApps = config.desktop_allowed_apps
    .filter((app) => !fs.existsSync(app.executable))
    .map((app) => app.id);
  if (config.desktop_enabled && missingApps.length > 0) {
    throw new Error('configured desktop app does not exist');
  }
  process.stdout.write(`${JSON.stringify({ status: 'VALID', desktop_enabled: config.desktop_enabled })}\n`);
} catch {
  process.stdout.write(`${JSON.stringify({ status: 'INVALID' })}\n`);
  process.exitCode = 3;
}
