# Marked

- Version: **18.1.0** (MIT)
- Upstream: https://github.com/markedjs/marked
- Release: https://registry.npmjs.org/marked/-/marked-18.1.0.tgz
- Release integrity: `sha512-PamYXWWWg2nboG3oX5Ffzpy3EFZROqZJK9hSlMmUnekp7TxPs5DcT95vwd46m73/exfKCRVMtgXoPMI4FEJL6w==`

`marked.umd.js` is the unmodified browser distribution; `marked.LICENSE` is
the upstream license. No install or frontend build step is required. Only the
explicit script asset is publicly served, not this directory.

The console uses `marked.lexer()` only. `../markdown.js` creates a restricted DOM
from its tokens and never uses Marked's HTML output. When updating, verify the
release integrity, retain the license, and run Markdown security/runtime tests
and the real-browser checks before changing the pinned version.
