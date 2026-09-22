"""FastMCP 挂载 FastAPI 的 HTTP 集成测试：streamable HTTP 端点 /mcp 完整握手调通 hello。"""
import json

from fastapi.testclient import TestClient

RPC_HEADERS = {"Accept": "application/json, text/event-stream"}


def _parse_rpc_response(resp) -> dict:
    """streamable-http 响应可能是 JSON 或 SSE（data: 行），统一解析出 JSON-RPC 对象。"""
    if resp.headers["content-type"].startswith("text/event-stream"):
        data_line = next(
            line for line in resp.text.splitlines() if line.startswith("data:")
        )
        return json.loads(data_line[len("data:") :])
    return resp.json()


def _initialize(http: TestClient) -> dict[str, str]:
    """完成 MCP initialize 握手，返回带会话头的请求头。"""
    resp = http.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0.0.0"},
            },
        },
        headers=RPC_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    headers = {**RPC_HEADERS, "Mcp-Session-Id": resp.headers["mcp-session-id"]}
    http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    return headers


def test_mcp_http_handshake_calls_hello():
    from app.main import app

    with TestClient(app) as http:  # 上下文进入时运行 lifespan（启动 session manager）
        headers = _initialize(http)
        call = http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "hello", "arguments": {"name": "世界"}},
            },
            headers=headers,
        )
        assert call.status_code == 200, call.text
        payload = _parse_rpc_response(call)
        content = payload["result"]["structuredContent"]
        assert content["success"] is True
        assert content["data"] == {"greeting": "你好，世界！"}


def test_mcp_http_list_tools():
    from app.main import app

    with TestClient(app) as http:
        headers = _initialize(http)
        resp = http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            headers=headers,
        )
        assert resp.status_code == 200
        payload = _parse_rpc_response(resp)
        names = [tool["name"] for tool in payload["result"]["tools"]]
        assert "hello" in names
