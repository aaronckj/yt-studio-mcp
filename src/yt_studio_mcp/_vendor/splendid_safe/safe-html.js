/* splendid-safe safe-html.js: HTML built from data is escaped by construction.
 *   esc(s)      text -> HTML-escaped text (& < > " ' `)
 *   attr(s)     same escaping, for a quoted attribute value
 *   safeUrl(u)  http(s) and relative URLs only; anything else (javascript:, data:, vbscript:, //host) -> "about:blank"
 *   html`...`   tagged template: every ${value} is escaped unless it is itself html`` / raw(); arrays are joined;
 *               null/undefined/false render as "". Returns a SafeHTML object, not a string.
 *   raw(s)      mark a TRUSTED, already-safe string as HTML (never pass data to it)
 *   setHTML(el, h)  el.innerHTML = h, refusing anything that is not SafeHTML (a plain string throws)
 * Plain script (window.SafeHTML + bare globals) and CommonJS (node tests). Vendored: change it in aaron/splendid-safe. */
(function (root) {
  'use strict';
  var MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"'`]/g, function (c) { return MAP[c]; }); }
  function attr(s) { return esc(s); }
  function SafeHTML(s) { this.__html = String(s); }
  SafeHTML.prototype.toString = function () { return this.__html; };
  function raw(s) { return new SafeHTML(s == null ? '' : s); }
  function isSafe(v) { return v instanceof SafeHTML; }
  function safeUrl(u) {
    var s = String(u == null ? '' : u).trim();
    // strip what browsers ignore inside a scheme ("java\tscript:", leading controls) before deciding
    var probe = s.replace(/[\u0000- \u007f-\u009f]/g, '').toLowerCase();
    if (/^https?:\/\//.test(probe)) return s;
    if (/^[a-z][a-z0-9+.\-]*:/.test(probe)) return 'about:blank';      // any other scheme
    if (/^[\\/]{2}/.test(probe)) return 'about:blank';                   // protocol-relative //host, \\host, /\host
    if (/[\u0000-\u001f\u007f]/.test(s)) return 'about:blank';
    return s;                                                             // relative: /x, x, ?q, #h
  }
  function piece(v) {
    if (v == null || v === false) return '';
    if (isSafe(v)) return v.__html;
    if (Array.isArray(v)) return v.map(piece).join('');
    return esc(v);
  }
  function html(strings) {
    var out = strings[0];
    for (var i = 1; i < strings.length; i++) out += piece(arguments[i]) + strings[i];
    return new SafeHTML(out);
  }
  function setHTML(el, h) {
    if (!isSafe(h)) throw new TypeError('setHTML needs html`` or raw(), not a plain string');
    el.innerHTML = h.__html;
    return el;
  }
  var api = { esc: esc, attr: attr, safeUrl: safeUrl, html: html, raw: raw, setHTML: setHTML, isSafe: isSafe, SafeHTML: SafeHTML };
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) {
    root.SafeHTML = api;
    ['esc', 'attr', 'safeUrl', 'html', 'raw', 'setHTML'].forEach(function (k) { if (!(k in root)) root[k] = api[k]; });
  }
})(typeof window !== 'undefined' ? window : null);
