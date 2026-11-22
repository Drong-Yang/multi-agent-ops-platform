"""Call the local platform using the official MCP Streamable HTTP client."""

import argparse
import asyncio
import getpass
import json
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8010")
    parser.add_argument("--username", required=True, help="使用已注册的工作台账号")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="搜索商品和政策原始资料")
    search.add_argument("query")
    search.add_argument("--sku")
    draft = commands.add_parser("draft", help="为已有 Wiki 任务生成待审草稿")
    draft.add_argument("task_id")
    draft.add_argument("--evidence-id", action="append", required=True)
    index = commands.add_parser("reindex", help="为预置的 Milvus 快照提交重建任务")
    index.add_argument("snapshot_id")
    index.add_argument("idempotency_key")
    job = commands.add_parser("job", help="查询自己的索引任务")
    job.add_argument("job_id")
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (url.scheme not in ("http", "https") or url.hostname not in ("localhost", "127.0.0.1", "::1")
            or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment):
        parser.error("--url 必须是本机 API 的根地址")
    return args


def invocation(args):
    if args.command == "search":
        params = {"query": args.query}
        if args.sku:
            params["sku"] = args.sku
        return "knowledge", "search_knowledge", params
    if args.command == "draft":
        return "operations", "create_wiki_draft", {"task_id": args.task_id, "evidence_ids": args.evidence_id}
    if args.command == "reindex":
        return "maintenance", "request_reindex", {
            "snapshot_id": args.snapshot_id, "idempotency_key": args.idempotency_key,
        }
    return "maintenance", "get_reindex_job", {"job_id": args.job_id}


async def run(args, password):
    scope, tool, params = invocation(args)
    base = args.url.rstrip("/")
    async with httpx.AsyncClient(timeout=35, trust_env=False) as http:
        login = await http.post(f"{base}/api/v1/auth/login", json={"username": args.username, "password": password})
        login.raise_for_status()
        token = await http.post(
            f"{base}/api/v1/mcp/token", json={"scope": scope},
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        token.raise_for_status()
        http.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
        async with streamable_http_client(f"{base}/mcp/{scope}/", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, params)
    print(json.dumps(result.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2))
    return 1 if result.isError else 0


if __name__ == "__main__":
    args = arguments()
    try:
        raise SystemExit(asyncio.run(run(args, getpass.getpass("工作台密码: "))))
    except httpx.HTTPStatusError as exc:
        raise SystemExit(f"请求失败，HTTP {exc.response.status_code}，请核对账号或服务配置") from None
    except httpx.ConnectError:
        raise SystemExit("无法连接本机服务，请先启动工作台后端") from None
