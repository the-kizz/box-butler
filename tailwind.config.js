// Content scanning config. The vendored standalone Tailwind CLI in this
// repo (.tools/tailwindcss) is v4, which reads content globs from an
// @source directive in the CSS (see boxbutler/web/static/src/input.css)
// rather than from this file's `content` key. This file is kept for
// tooling that still expects it (editors, older CLIs) and as the single
// documented source of the glob.
module.exports = {
  content: ["boxbutler/web/templates/**/*.html"],
};
