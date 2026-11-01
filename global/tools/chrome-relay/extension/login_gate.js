/*
Fleet Chrome Relay fixed saved-login page function (classic importScripts).
Integrates the proven login_gate.cjs inspection. Agent arguments contain no
credentials, selectors, coordinates, JavaScript or CDP method; sw.js passes
only the owner binding (origin, page and POST paths) and a fixed mode.
The function NEVER reads input values, password storage, cookies, headers,
account names, HTML or network responses: only form shape, autofill state
(:-webkit-autofill) and the submit button's centre.
*/

'use strict';

// Self-contained: chrome.scripting serializes it into the page's isolated world.
function loginFormProbe(command) {
  const KEY = '__fleetRelayLoginGuard';
  function fail(reason) { return { eligible: false, reason: reason }; }
  try {
    if (!command || typeof command !== 'object' || ['inspect', 'arm', 'status', 'disarm'].indexOf(command.mode) === -1) return fail('unsupported_login');
    // Prototype getters: named form controls cannot clobber them.
    function getter(proto, name) { return Object.getOwnPropertyDescriptor(proto, name).get; }
    const formElements = getter(HTMLFormElement.prototype, 'elements');
    const formAction = getter(HTMLFormElement.prototype, 'action');
    const formMethod = getter(HTMLFormElement.prototype, 'method');
    const formTarget = getter(HTMLFormElement.prototype, 'target');
    const hasAttr = Element.prototype.hasAttribute;
    const tagName = getter(Element.prototype, 'tagName');
    const query = Document.prototype.querySelectorAll;
    const prior = window[KEY];
    if (command.mode === 'status' || command.mode === 'disarm') {
      if (!prior || typeof prior.outcome !== 'string') return { outcome: 'absent' };
      prior.disarm();
      return { outcome: prior.outcome };
    }
    if (prior && typeof prior.disarm === 'function') prior.disarm();
    if (window.top !== window) return fail('unsupported_login');
    function visible(el) { return !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden'; }
    function controlType(el) { return String(el.type || '').toLowerCase(); }
    function isButton(el) {
      const tag = tagName.call(el);
      return (tag === 'BUTTON' && controlType(el) === 'submit') || (tag === 'INPUT' && controlType(el) === 'submit');
    }
    const passwordsAnywhere = Array.from(query.call(document, 'input')).filter(function (el) { return controlType(el) === 'password' && visible(el); });
    const forms = Array.from(query.call(document, 'form')).filter(function (form) {
      return Array.from(formElements.call(form)).some(function (el) { return controlType(el) === 'password' && visible(el); });
    });
    if (forms.length !== 1 || passwordsAnywhere.length !== 1) return fail('unsupported_login');
    const form = forms[0];
    const fields = Array.from(formElements.call(form)).filter(visible);
    const passwords = fields.filter(function (el) { return controlType(el) === 'password'; });
    const usernames = fields.filter(function (el) { return tagName.call(el) === 'INPUT' && ['text', 'email'].indexOf(controlType(el)) !== -1; });
    const buttons = fields.filter(isButton);
    if (passwords.length !== 1 || passwords[0] !== passwordsAnywhere[0] || usernames.length > 1 || buttons.length !== 1) return fail('unsupported_login');
    const button = buttons[0];
    if (fields.some(function (el) { return /one-time-code|new-password|webauthn/i.test(String(el.autocomplete || '')); })) return fail('unsupported_login');
    if (query.call(document, 'iframe[src*="captcha"],iframe[src*="challenge"],[data-sitekey]').length) return fail('unsupported_login');
    const inlineSubmit = hasAttr.call(form, 'onsubmit');
    const inlineClick = hasAttr.call(button, 'onclick');
    // Inspection may describe inline handlers, but arming accepts them only
    // when SW explicitly says it has disabled page-script execution in CDP.
    if ((inlineSubmit || inlineClick) && command.mode !== 'inspect' && command.allowInline !== true) return fail('unsupported_login');
    if (['formaction', 'formmethod', 'formtarget', 'formenctype', 'formnovalidate'].some(function (name) { return hasAttr.call(button, name); })) return fail('unsupported_login');
    const method = String(formMethod.call(form)).toLowerCase();
    const targetName = String(formTarget.call(form));
    if (method !== 'post' || (targetName && targetName !== '_self')) return fail('unsupported_login');
    const action = String(formAction.call(form));
    const target = new URL(action, location.href);
    if (target.search || target.hash || target.username || target.password) return fail('login_route');
    const pages = Array.isArray(command.pages) ? command.pages : [];
    const posts = Array.isArray(command.posts) ? command.posts : [];
    if (location.origin !== command.origin || target.origin !== command.origin || pages.indexOf(location.pathname) === -1 || posts.indexOf(target.pathname) === -1 || location.search || location.hash) return fail('login_route');
    const rect = button.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    // Avoid click-through: the button itself must be at its fixed centre.
    const hit = document.elementFromPoint(x, y);
    if (button.disabled || !(hit === button || button.contains(hit)) || !(x >= 0 && y >= 0 && x < innerWidth && y < innerHeight)) return fail('unsupported_login');
    const savedPassword = passwords[0].matches(':-webkit-autofill');
    const savedUsername = usernames.length === 0 || usernames[0].matches(':-webkit-autofill');
    const saved = savedPassword && savedUsername;
    if (!saved) return fail('needs_owner');
    const result = { eligible: true, origin: location.origin, path: location.pathname, target_origin: target.origin, target_path: target.pathname, saved: saved, x: x, y: y, inline_submit: inlineSubmit, inline_click: inlineClick };
    if (command.mode === 'inspect') return result;
    // Fixed capture-submit guard for this one form, button and destination.
    const expectedAction = target.href;
    const expectedPath = location.pathname;
    const deadline = typeof command.deadline === 'number' ? command.deadline : 0;
    const guard = { outcome: 'none', submitter: null, disarm: null };
    let cleanupTimer = null;
    function intact(submitter) {
      if (submitter !== button || !form.isConnected || location.pathname !== expectedPath) return false;
      if (['formaction', 'formmethod', 'formtarget', 'formenctype', 'formnovalidate'].some(function (name) { return hasAttr.call(button, name); })) return false;
      const nowTarget = String(formTarget.call(form));
      return String(formAction.call(form)) === expectedAction && String(formMethod.call(form)).toLowerCase() === 'post' && (!nowTarget || nowTarget === '_self');
    }
    function active(event) { return event.target === form && Date.now() <= deadline; }
    function onSubmit(event) {
      if (!active(event) || guard.outcome !== 'none') return;
      guard.submitter = event.submitter || null;
      if (!intact(guard.submitter)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        guard.outcome = 'form_changed';
        return;
      }
      guard.outcome = 'submitting';
    }
    function onSubmitDone(event) {
      // A page handler cancelling native submission means JS-only auth.
      if (event.target === form && event.defaultPrevented && (guard.outcome === 'submitting' || guard.outcome === 'submitted')) guard.outcome = 'prevented';
    }
    function onFormData(event) {
      if (!active(event)) return;
      if (guard.outcome === 'submitting' && intact(guard.submitter)) { guard.outcome = 'submitted'; return; }
      if (guard.outcome === 'submitted' || guard.outcome === 'prevented') return;
      // Observe only. Never alter FormData or a later manual submit.
      guard.outcome = 'form_changed';
    }
    window.addEventListener('submit', onSubmit, true);
    window.addEventListener('submit', onSubmitDone, false);
    window.addEventListener('formdata', onFormData, true);
    guard.disarm = function () {
      if (cleanupTimer !== null) clearTimeout(cleanupTimer);
      cleanupTimer = null;
      window.removeEventListener('submit', onSubmit, true);
      window.removeEventListener('submit', onSubmitDone, false);
      window.removeEventListener('formdata', onFormData, true);
      if (window[KEY] === guard) delete window[KEY];
    };
    window[KEY] = guard;
    cleanupTimer = setTimeout(guard.disarm, Math.max(1, Math.min(5000, deadline - Date.now())));
    return Object.assign({ armed: true }, result);
  } catch (err) {
    return fail('unsupported_login');
  }
}

globalThis.FleetLoginGate = Object.freeze({ loginFormProbe: loginFormProbe });
