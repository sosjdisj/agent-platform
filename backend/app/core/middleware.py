"""安全响应头中间件：为所有 HTTP 响应附加安全头（纯 ASGI 实现，无请求体拷贝开销）。

CSP 说明：
- script-src 'self'：Vite 构建产物为外链模块脚本，无内联脚本
- style-src 'unsafe-inline'：Element Plus / Vue 动态样式与 index.html 首屏骨架需要内联样式
- connect-src 'self'：API 请求与 SSE 均为同源
- /docs /redoc /openapi.json 为 Swagger UI（默认走 CDN），豁免 CSP（仅 debug 模式开启）
"""
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Swagger / ReDoc 文档路径（其静态资源依赖 CDN 与内联脚本，不适用严格 CSP）
_DOCS_PREFIXES = ("/docs", "/redoc", "/openapi.json")

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    """附加安全响应头：CSP / nosniff / 反点击劫持 / 引用策略 / 权限策略 / HSTS。"""

    def __init__(self, app: ASGIApp, enable_hsts: bool = False) -> None:
        self.app = app
        # HSTS 仅在 HTTPS 部署时开启（配置项见 Settings.enable_hsts）
        self.headers: list[tuple[str, str]] = [
            (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
            (b"cross-origin-opener-policy", b"same-origin"),
        ]
        if enable_hsts:
            self.headers.append(
                (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 文档页豁免 CSP（其余安全头仍然附加）
        headers = [
            h for h in self.headers
            if not (h[0] == b"content-security-policy" and scope["path"].startswith(_DOCS_PREFIXES))
        ]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)
