"""MCP（Model Context Protocol）模块。

- base.py：工具元数据基类与统一错误结构
- registry.py：工具注册表（挂载到 FastMCP）
- server.py：FastMCP Server 构建（FastAPI 进程内集成，端点 /mcp）
- client.py：MCPClient 统一调用封装（异步 / 超时 / 错误归一化）
- tools/：链路验证工具（hello）与注册聚合
- database/ business/ knowledge/：业务工具子模块（骨架，随后续 Prompt 接入）
"""
