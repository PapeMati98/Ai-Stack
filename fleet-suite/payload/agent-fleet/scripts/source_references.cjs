'use strict';
// Parse source as data only. Never load a project's tsconfig, imports or plugins.
const fs = require('node:fs');
const crypto = require('node:crypto');
const path = require('node:path');
const modulePath = path.join(__dirname, 'vendor/typescript/typescript.js');
const moduleBytes = fs.readFileSync(modulePath);
if (crypto.createHash('sha256').update(moduleBytes).digest('hex') !==
    '3ae902c92cc44dace175c0e69e13a4b0899f6983c6121d76b9ab8dd5795e7675') {
  throw new Error('Parser integrity mismatch');
}
const ts = require(modulePath);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
if (typeof input.text !== 'string' || Buffer.byteLength(input.text) > 2 * 1024 * 1024) {
  throw new Error('Unbounded source');
}
const text = input.text;
const sf = ts.createSourceFile('source' + input.extension, text, ts.ScriptTarget.Latest, true,
  input.extension === '.tsx' ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
const ranges = [];
function reference(node) {
  if (!ts.isPropertyAccessExpression(node) || node.questionDotToken ||
      !ts.isIdentifier(node.name)) return false;
  const base = node.expression;
  return ts.isIdentifier(base) || base.kind === ts.SyntaxKind.ThisKeyword || reference(base);
}
function visit(node) {
  // Text containers are not proof of source assignments, including template expressions.
  if (ts.isTemplateExpression(node) || ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) return;
  let name, value;
  if (ts.isVariableDeclaration(node) || ts.isPropertyDeclaration(node) || ts.isPropertyAssignment(node)) {
    name = node.name; value = node.initializer;
  } else if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
    name = ts.isPropertyAccessExpression(node.left) ? node.left.name : node.left;
    value = node.right;
  }
  if (name && ts.isIdentifier(name) && value && reference(value) && ranges.length < 8192) {
    const start = name.getStart(sf), vstart = value.getStart(sf), end = value.end;
    if (/^\s*[:=]\s*$/.test(text.slice(name.end, vstart)) &&
        /^(?:this|[A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)+$/.test(text.slice(vstart, end))) {
      ranges.push([Buffer.byteLength(text.slice(0, start)), Buffer.byteLength(text.slice(0, end))]);
    }
  }
  ts.forEachChild(node, visit);
}
if (sf.parseDiagnostics.length === 0) visit(sf);
process.stdout.write(JSON.stringify({version: ts.version, valid: sf.parseDiagnostics.length === 0, ranges}));
