# AICP 接入

本机 AICP 的 MCP 端点：http://127.0.0.1:9100/mcp（server 名 aicp）。
健康检查：curl http://127.0.0.1:9100/health — 不通说明引擎没起。

连上后 initialize 返回的 instructions 已内含操作手册，不必先读本地文档。
需要更细的插件用法时读：E:\aicp-skills\data\skills\aicp\aicp-mcp-bridge\SKILL.md

三条铁律：
1. receiver 必须是完整路由（builtins/tools/code_runner、os/_cron、applications/<proj>/<name>），写插件名会 plugin not found。
2. 有副作用的动作（删文件、发布应用、改系统配置）先问用户。
3. 端点只绑 127.0.0.1，不要转发到公网。

端点不通时：python "E:\aicp-skills\data\skills\aicp\aicp-mcp-bridge\scripts\aicp_mcp.py" ensure
