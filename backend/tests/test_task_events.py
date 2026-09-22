"""任务事件流 SSE（20.1）验收测试。

验收：cookie 无效 401、他人 task_id 403、正常连接收到测试事件、断线重连。
SSE 为无限流：httpx 的 ASGITransport 会等 ASGI 应用执行完毕才返回响应，
无法承载流式断言，故流式路径经 SseStream 探针直接驱动 ASGI 协议；
非流式错误路径复用 api_client。不做：真实 Trace 事件接线（20.2）。
"""
import asyncio
import json

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.mcp.base import RiskLevel
from app.models.agent import AgentTask
from app.schemas.events import ApprovalRequiredEvent
from app.services.task_events import task_channel

from tests.test_auth import PASSWORD, _error_body, login, register

settings = get_settings()

EVENTS_URL = "/api/tasks/{task_id}/events"


async def _make_task(maker: async_sessionmaker, user_id: int) -> int:
    """直插一条属于 user_id 的任务，返回任务 id。"""
    async with maker() as session:
        task = AgentTask(user_id=user_id, title="分析客户A", query="帮我分析客户A销售额下降的原因")
        session.add(task)
        await session.commit()
        return task.id


def _make_event(task_id: int, user_id: int, approval_id: int) -> ApprovalRequiredEvent:
    return ApprovalRequiredEvent(
        task_id=task_id,
        approval_id=approval_id,
        tool="update_customer",
        risk_level=RiskLevel.HIGH,
        requester_id=user_id,
        parameter_summary='{"contact_name": "张三"}',
    )


async def _wait_subscribed(stream: "SseStream", timeout: float = 5.0) -> None:
    """读到首帧心跳即视为订阅已生效。

    生成器首轮 get_message 必然先消费 SUBSCRIBE 确认帧（确认帧在 subscribe 发送时
    同步入队）→ 返回 None → 首帧必为心跳，与「订阅已被服务端处理」一一对应，
    无需轮询 PUBSUB（fakeredis 的 numsub 返回 [(channel, count)] 元组形态，且
    aclose 不即时清退订表，均不可靠）。
    """
    block = await stream.read_block(timeout)
    assert block == ": heartbeat", f"预期首帧为心跳，实际: {block!r}"


async def _read_event(stream: "SseStream", timeout: float = 5.0) -> str:
    """跳过心跳注释行读出首个事件帧（SSE 客户端对注释行的标准忽略行为）。

    订阅确认帧会使生成器首轮轮询返回 None 并先发一帧心跳，故等事件必须跳过注释帧。
    """
    while True:
        block = await stream.read_block(timeout)
        if any(not line.startswith(":") for line in block.splitlines()):
            return block


def _parse_event(block: str) -> tuple[str, str]:
    """解析 SSE 帧为 (event, data)；心跳等注释行被 EventSource 忽略，此处跳过。"""
    event, data = "message", ""
    for line in block.splitlines():
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "event":
            event = value.strip()
        elif field == "data":
            data = value.strip()
    return event, data


class SseStream:
    """直接驱动 ASGI 协议的 SSE 流探针：自持 receive/send，可读帧、可模拟断开。"""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._buf = ""
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._request_sent = False
        self._task: asyncio.Task | None = None

    async def _receive(self) -> dict:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        else:
            await self._chunks.put(message.get("body", b""))

    async def open(self, path: str, token: str) -> "SseStream":
        """以 SSE Cookie 认证发起请求，等待响应开始（认证与归属校验已完成）。"""
        from app.main import app

        scope = {
            "type": "http",
            # 不带 spec_version → starlette 走 listen_for_disconnect 分支，可测断开
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "scheme": "http",
            "root_path": "",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 123),
            "headers": [
                (b"host", b"testserver"),
                (b"cookie", f"{settings.sse_cookie_name}={token}".encode()),
            ],
        }
        self._task = asyncio.create_task(app(scope, self._receive, self._send))
        for _ in range(500):
            if self.status is not None:
                return self
            await asyncio.sleep(0.01)
        raise TimeoutError("SSE 响应未开始")

    async def read_block(self, timeout: float = 5.0) -> str:
        """读出一条完整 SSE 帧（空行分隔的原文）。"""
        while "\n\n" not in self._buf:
            chunk = await asyncio.wait_for(self._chunks.get(), timeout)
            self._buf += chunk.decode()
        block, self._buf = self._buf.split("\n\n", 1)
        return block

    async def disconnect(self) -> None:
        """模拟客户端断开，并等待服务端收尾（生成器退订、应用调用返回）；幂等可重入。"""
        if self._task is None:
            return
        self._disconnect.set()
        try:
            await asyncio.wait_for(self._task, 5)
        finally:
            self._task = None


# ---------- 认证与归属 ----------


async def test_missing_or_invalid_cookie_401(api_client):
    """无 SSE Cookie / Cookie 无效 → 401 + 结构化错误体。"""
    resp = await api_client.get(EVENTS_URL.format(task_id=1))
    assert resp.status_code == 401
    assert _error_body(resp)["error"]["code"] == "TOKEN_MISSING"

    resp = await api_client.get(
        EVENTS_URL.format(task_id=1), cookies={settings.sse_cookie_name: "garbage"}
    )
    assert resp.status_code == 401
    assert _error_body(resp)["error"]["code"] == "TOKEN_INVALID"


async def test_other_users_task_403_and_missing_404(api_client, db_session_maker):
    """他人任务 → 403 TASK_FORBIDDEN；任务不存在 → 404 TASK_NOT_FOUND。"""
    owner = (await register(api_client)).json()
    await register(api_client, username="bob", email="bob@example.com")
    await login(api_client, "bob", PASSWORD)  # 切换为用户 bob 的 Cookie

    task_id = await _make_task(db_session_maker, owner["id"])
    resp = await api_client.get(EVENTS_URL.format(task_id=task_id))
    assert resp.status_code == 403
    assert _error_body(resp)["error"]["code"] == "TASK_FORBIDDEN"

    resp = await api_client.get(EVENTS_URL.format(task_id=424242))
    assert resp.status_code == 404
    assert _error_body(resp)["error"]["code"] == "TASK_NOT_FOUND"


# ---------- 事件流推送 ----------


async def test_stream_receives_published_event(api_client, db_session_maker, fake_redis):
    """本人任务：SSE 连接建立后，Pub/Sub 发布的事件以同构 SSE 帧推送给订阅者。"""
    user_id = (await register(api_client)).json()["id"]
    await login(api_client)
    task_id = await _make_task(db_session_maker, user_id)
    token = api_client.cookies.get(settings.sse_cookie_name)

    stream = SseStream()
    try:
        await stream.open(EVENTS_URL.format(task_id=task_id), token)
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert stream.headers["cache-control"] == "no-cache"

        await _wait_subscribed(stream)
        event = _make_event(task_id, user_id, approval_id=1)
        assert await fake_redis.publish(task_channel(task_id), event.model_dump_json()) == 1

        block = await _read_event(stream)
        assert block == f"event: approval_required\ndata: {event.model_dump_json()}"
        name, data = _parse_event(block)
        assert name == "approval_required"
        assert json.loads(data)["tool"] == "update_customer"
    finally:
        await stream.disconnect()


async def test_heartbeat_when_idle(api_client, db_session_maker, monkeypatch):
    """无事件空档期发送注释行心跳保活。"""
    monkeypatch.setattr(settings, "sse_heartbeat_seconds", 0.05)
    user_id = (await register(api_client)).json()["id"]
    await login(api_client)
    task_id = await _make_task(db_session_maker, user_id)
    token = api_client.cookies.get(settings.sse_cookie_name)

    stream = SseStream()
    try:
        await stream.open(EVENTS_URL.format(task_id=task_id), token)
        assert await stream.read_block() == ": heartbeat"
    finally:
        await stream.disconnect()


async def test_disconnect_then_reconnect(api_client, db_session_maker, fake_redis):
    """断线：连接释放不再收事件；重连：新连接重新订阅并继续收到后续事件。"""
    user_id = (await register(api_client)).json()["id"]
    await login(api_client)
    task_id = await _make_task(db_session_maker, user_id)
    token = api_client.cookies.get(settings.sse_cookie_name)
    channel = task_channel(task_id)

    stream = SseStream()
    resumed = SseStream()
    try:
        await stream.open(EVENTS_URL.format(task_id=task_id), token)
        await _wait_subscribed(stream)
        first = _make_event(task_id, user_id, approval_id=1)
        assert await fake_redis.publish(channel, first.model_dump_json()) == 1
        assert _parse_event(await _read_event(stream))[0] == "approval_required"

        await stream.disconnect()  # 客户端断开，服务端退订收尾

        # 断线期间丢发的事件不应补发：重连后首个事件帧必须是重连后发布的 latest，
        # 若读到 lost 即为缺陷（lost 的 publish 返回值不断言：fakeredis 的 aclose
        # 不即时清退订表，滞留的弱引用条目仍会计入 receivers，属实现细节）
        lost = _make_event(task_id, user_id, approval_id=2)
        await fake_redis.publish(channel, lost.model_dump_json())

        # 断线重连：新连接独立订阅，此后的事件正常推送
        await resumed.open(EVENTS_URL.format(task_id=task_id), token)
        await _wait_subscribed(resumed)
        latest = _make_event(task_id, user_id, approval_id=3)
        assert await fake_redis.publish(channel, latest.model_dump_json()) >= 1
        name, data = _parse_event(await _read_event(resumed))
        assert name == "approval_required"
        assert json.loads(data) == latest.model_dump(mode="json")
    finally:
        # 两个流都必须收尾：泄漏的挂起生成器会让 pytest-asyncio 关闭事件循环时卡死
        await stream.disconnect()
        await resumed.disconnect()
