// Preloaded into the installed Playwright Node driver, before its HTTP client.
// route.fetch() otherwise collects an unlimited response before returning.
'use strict';
const limit = 8 * 1024 * 1024;
const failure = () => new Error('browser_worker_response_too_large');

function bounded(stream, cancel = () => {}) {
  const push = stream.push;
  let size = 0;
  stream.push = function (chunk, encoding) {
    if (chunk !== null) {
      const length = Buffer.isBuffer(chunk) || chunk instanceof Uint8Array ?
        chunk.length : Buffer.byteLength(chunk, encoding);
      if (size + length > limit) {
        cancel();
        this.destroy(failure());
        return false;
      }
      size += length;
    }
    return push.call(this, chunk, encoding);
  };
  return stream;
}

for (const moduleName of ['node:http', 'node:https']) {
  const transport = require(moduleName);
  const request = transport.request;
  transport.request = function (...args) {
    // Install guards before the consumer's response callback can buffer data.
    const callback = typeof args.at(-1) === 'function' ? args.pop() : null;
    const child = request.apply(this, args);
    child.prependListener('response', response => {
      // Playwright's async callback can await cookie storage before attaching
      // its response error listener. The request error still rejects the fetch.
      response.on('error', () => {});
      const length = response.headers['content-length'];
      if (length !== undefined && (!/^\d+$/.test(length) || BigInt(length) > BigInt(limit))) {
        // Defer destruction until the consumer has registered error handlers.
        queueMicrotask(() => { child.destroy(failure()); response.destroy(failure()); });
      }
      bounded(response, () => child.destroy(failure()));
    });
    if (callback) child.on('response', callback);
    return child;
  };
}

// Limit decoded bytes as well, including gzip/deflate/brotli bombs whose wire
// size fits the limit. pipeline() propagates decoder errors to the HTTP stream.
const zlib = require('node:zlib');
for (const name of ['createGunzip', 'createInflate', 'createBrotliDecompress']) {
  const create = zlib[name];
  Object.defineProperty(zlib, name, {
    ...Object.getOwnPropertyDescriptor(zlib, name),
    value: function (...args) { return bounded(create.apply(this, args)); },
  });
}
require('node:module').syncBuiltinESMExports();
