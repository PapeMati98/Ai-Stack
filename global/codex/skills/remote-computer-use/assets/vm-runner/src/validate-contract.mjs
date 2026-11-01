import fs from 'node:fs';
import process from 'node:process';

import { validateConfig, validateContract } from './policy.mjs';

const [contractPath, configPath] = process.argv.slice(2);

try {
  if (!contractPath || !configPath) {
    throw new Error('contract and config paths are required');
  }
  const config = validateConfig(JSON.parse(fs.readFileSync(configPath, 'utf8')));
  const contract = validateContract(JSON.parse(fs.readFileSync(contractPath, 'utf8')), config);
  process.stdout.write(`${JSON.stringify({ status: 'VALID', mode: contract.mode })}\n`);
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  process.stdout.write(`${JSON.stringify({ status: 'INVALID', error: message.slice(0, 1000) })}\n`);
  process.exitCode = 3;
}
