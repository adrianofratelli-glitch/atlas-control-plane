"""Torre MCP server over stdio. No write execution tool is exposed."""
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, ToolAnnotations, TextContent, CallToolResult
from starlette.concurrency import run_in_threadpool
from assistant_tools import TOOLS, ClusterTools

load_dotenv(Path(__file__).with_name('.env'))
server = Server("torre_mcp")


def bounded_result(result, arguments, budget=32000):
    """Keep JSON intact and make every omitted page item retrievable."""
    if len(json.dumps(result)) <= budget:
        return result
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        items = list(result["items"])
        while items:
            candidate = {**result, "items": items, "has_more": True,
                         "next_offset": arguments.get("offset", 0) + len(items),
                         "page_reduced": True}
            if len(json.dumps(candidate)) <= budget:
                return candidate
            items.pop()
    raise ValueError("Resultado excede 32 KB. Reduza a projeção, período ou complexidade da consulta.")


@server.list_tools()
async def list_tools():
    return [Tool(name=t["name"], description=t["description"], inputSchema=t["input_schema"],
                 annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False)) for t in TOOLS.values()]


@server.call_tool()
async def call_tool(name, arguments):
    try:
        result = await run_in_threadpool(ClusterTools(*sys.argv[1:3]).call, name, arguments)
        result = bounded_result(result, arguments)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))], isError=False)
    except Exception as exc:
        from fastapi import HTTPException
        from jsonschema import ValidationError
        if isinstance(exc, HTTPException):
            message = str(exc.detail)
        elif isinstance(exc, ValidationError):
            message = "Argumentos inválidos para a ferramenta; confira seu schema."
        elif isinstance(exc, ValueError):
            message = str(exc)
        else:
            message = "Falha na consulta ao cluster. Verifique conexão, permissões e estado. Nenhuma alteração foi executada."
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"error": message}))], isError=True)


async def main():
    if len(sys.argv) != 3:
        raise SystemExit("Uso: torre_mcp_server.py PROJECT_ID CLUSTER_NAME")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
