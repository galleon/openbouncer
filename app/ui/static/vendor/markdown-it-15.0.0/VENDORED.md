Vendored from https://registry.npmjs.org/markdown-it/-/markdown-it-15.0.0.tgz
(sha1 dc199771f75b01d792316e5b524855b3973868e2),
`dist/browser/markdown-it.umd.min.js` (renamed to `markdown-it.min.js`) and
`LICENSE` copied in unmodified. Self-hosted (no CDN) so the chat tester
page -- which holds a live bearer API key -- never makes a runtime request
to a third party. Exposes a global `markdownit()` factory function.

Used with default options (`html: false`, the default) so raw HTML in
model output is escaped, not rendered -- this page renders untrusted
model output, and markdown-it's own docs call out `html: true` as unsafe
without a separate sanitizer. Do not enable it.

To upgrade: download the new tarball, verify its shasum against npm's
registry metadata, and replace `markdown-it.min.js` with the new
version's `dist/browser/markdown-it.umd.min.js` the same way.
