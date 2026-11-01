// Loaded into Playwright's existing Node driver via NODE_OPTIONS on Windows.
// Playwright 1.63 hides taskkill but does not set windowsHide on browser spawn.
// Force the same policy on all child-process entry points and descendants.
'use strict';
const cp = require('node:child_process');
const path = require('node:path');
const hidden = (options, file = '', args = []) => {
  // The owner explicitly requested a visible Chromium GUI. Chrome's GUI
  // executable creates no console; keep every console/helper process hidden.
  const ownerGui = process.platform === 'win32' &&
    process.env.BROWSER_WORKER_HEADED === '1' &&
    path.basename(file).toLowerCase() === 'chrome.exe' &&
    args.some(arg => typeof arg === 'string' && arg.startsWith('--user-data-dir=')) &&
    !args.some(arg => typeof arg === 'string' && arg.startsWith('--headless'));
  return { ...(options || {}), windowsHide: !ownerGui };
};

for (const name of ['spawn', 'spawnSync', 'execFileSync', 'fork']) {
  const original = cp[name];
  cp[name] = function (file, args, options) {
    if (!Array.isArray(args)) {
      options = args;
      args = [];
    }
    return original.call(cp, file, args, hidden(options, file, args));
  };
}

const originalExecFile = cp.execFile;
cp.execFile = function (file, args, options, callback) {
  if (!Array.isArray(args)) {
    callback = typeof args === 'function' ? args :
      (typeof options === 'function' ? options : callback);
    options = typeof args === 'object' ? args : {};
    args = [];
  } else if (typeof options === 'function') {
    callback = options;
    options = {};
  }
  return originalExecFile.call(cp, file, args, hidden(options, file, args), callback);
};

const originalExec = cp.exec;
cp.exec = function (command, options, callback) {
  if (typeof options === 'function') {
    callback = options;
    options = {};
  }
  return originalExec.call(cp, command, hidden(options), callback);
};
const originalExecSync = cp.execSync;
cp.execSync = function (command, options) {
  return originalExecSync.call(cp, command, hidden(options));
};
require('node:module').syncBuiltinESMExports();
