"""api：薄 HTTP 层。

只做请求/响应校验与转发：依赖注入取 OpsAgent（request.app.state.agent），
不含业务逻辑；请求/响应体与对外契约保持旧 ops_bot 不变。
"""
