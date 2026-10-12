# Marked

- 版本： **18.1.0** (MIT)
- 上游：[Marked](https://github.com/markedjs/marked)
- 发布包：[18.1.0](https://registry.npmjs.org/marked/-/marked-18.1.0.tgz)
- 发布包完整性： `sha512-PamYXWWWg2nboG3oX5Ffzpy3EFZROqZJK9hSlMmUnekp7TxPs5DcT95vwd46m73/exfKCRVMtgXoPMI4FEJL6w==`

`marked.umd.js` 是未经修改的浏览器发行文件，`marked.LICENSE` 保留上游许可。
无需安装或前端构建。服务仅公开指定脚本，不公开整个目录。

控制台仅调用 `marked.lexer()`，由 `../markdown.js` 通过允许列表构建 DOM，不使用
Marked 的 HTML 输出。升级时核对发布包完整性、保留许可，并执行 Markdown 安全、
运行时与真实浏览器验收，再更新固定版本。
