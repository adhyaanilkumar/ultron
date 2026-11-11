"""Throwaway spike: 1-tool MCP server probing Claude Code host behaviour.

Run `python server.py` (stdio server) or `python server.py --self-test`.
"""

import asyncio
import sys

from mcp.server.fastmcp import Context, FastMCP
from pydantic import AnyUrl, BaseModel

mcp = FastMCP("spike")


class Approval(BaseModel):
    approve: bool
    comment: str = ""


@mcp.resource("spike://status")
def status() -> str:
    return "spike status: ok"


@mcp.tool()
async def spike_probe(ctx: Context) -> str:  # type: ignore[type-arg]
    """Probe host support: progress, resource-update and elicitation."""
    for i in range(1, 4):
        await ctx.report_progress(i, 3, f"spike progress {i}/3")
        await asyncio.sleep(1)
    try:
        await ctx.session.send_resource_updated(AnyUrl("spike://status"))
        notified = "resource-updated sent"
    except Exception as e:  # report, never crash the probe
        notified = f"resource-updated failed: {e!r}"
    try:
        res = await ctx.elicit("spike: approve this probe?", Approval)
        outcome = f"elicitation action={res.action}"
        if res.action == "accept":
            outcome += f" approve={res.data.approve} comment={res.data.comment!r}"
    except Exception as e:
        outcome = f"elicitation failed: {e!r}"
    return f"{notified}; {outcome}"


async def self_test() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[__file__])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        if "spike_probe" not in names:
            raise SystemExit(f"spike_probe missing: {names}")
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        try:
            asyncio.run(asyncio.wait_for(self_test(), timeout=30))
        except SystemExit:
            raise
        except BaseException as e:
            print(f"self-test failed: {e!r}", file=sys.stderr)
            sys.exit(1)
    else:
        mcp.run()
