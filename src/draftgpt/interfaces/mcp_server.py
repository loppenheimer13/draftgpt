"""MCP server -- lets a chat client call the prep tools conversationally.

Every tool is **read-only**. Nothing here mutates a fantasy platform, and the
only database writes are the recommendation-history rows the engine records for
replay.

Design note: tools return the same structured contract the CLI renders, not
prose. The assistant on the other end is expected to narrate from the returned
evidence, which keeps the "database is authoritative, the model explains"
separation intact across the wire -- an LLM that only ever sees computed
numbers cannot invent a projection.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from draftgpt.database.session import session_scope

logger = logging.getLogger(__name__)

SERVER_NAME = "draftgpt"
SERVER_INSTRUCTIONS = """\
Read-only fantasy football decision engine for one specific league.

Use these tools instead of general fantasy knowledge whenever the question is
about THIS league: scoring, roster construction, player value, or draft
strategy. Generic advice is frequently wrong here -- for example quarterback
value inverts between single-QB and superflex, and replacement level depends on
this league's exact starting slots.

Start with `prep_league` to learn the settings. Use `prep_draft_board` for the
ranked pool and `prep_player` for a single player. All values are already priced
under this league's scoring; do not re-rank them using outside rankings.

Every response carries `freshness` and `confidence`. Report degraded freshness
to the user rather than presenting stale numbers as current. Do not assert any
fact that is not present in the tool output.
"""


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


# --------------------------------------------------------------------------
# Tool implementations (pure functions over a session, so they are testable
# without an MCP transport).
# --------------------------------------------------------------------------
def tool_prep_league() -> dict:
    from draftgpt.commands.prep import prep_league

    with session_scope() as session:
        return json.loads(prep_league(session).model_dump_json())


def tool_prep_draft_board(
    position: str | None = None,
    limit: int = 30,
    at_pick: int | None = None,
    draft_position: int | None = None,
    tier_max: int | None = None,
) -> dict:
    from draftgpt.commands.prep import prep_draft

    with session_scope() as session:
        return json.loads(
            prep_draft(
                session,
                position=position,
                limit=limit,
                at_pick=at_pick,
                draft_position=draft_position,
                tier_max=tier_max,
            ).model_dump_json()
        )


def tool_prep_player(name: str, at_pick: int | None = None) -> dict:
    from draftgpt.commands.prep import prep_player
    from draftgpt.ingestion.identity import AmbiguousPlayerError

    with session_scope() as session:
        try:
            return json.loads(prep_player(session, name, at_pick=at_pick).model_dump_json())
        except AmbiguousPlayerError as exc:
            # Surfaced as data, not an exception: the assistant should ask the
            # user which player they meant rather than picking one.
            return {
                "error": "ambiguous_player",
                "message": str(exc),
                "candidates": [name for _, name in exc.candidates],
            }
        except LookupError as exc:
            return {"error": "not_found", "message": str(exc)}


def tool_roster_recommend(week: int, objective: str = "balanced") -> dict:
    from draftgpt.commands.roster import recommend_lineup
    from draftgpt.database.models import LeagueSeason

    with session_scope() as session:
        league_season = session.query(LeagueSeason).first()
        if league_season is None:
            return {"error": "no_league", "message": "no league synced"}
        return json.loads(
            recommend_lineup(
                session, league_season.id, week, objective=objective
            ).model_dump_json()
        )


def tool_league_status() -> dict:
    """League identity, sync state, and source freshness in one call."""
    from draftgpt.database.models import League, LeagueSeason, Player, Projection, Team
    from draftgpt.ingestion.runner import collect_freshness

    with session_scope() as session:
        league_season = session.query(LeagueSeason).first()
        if league_season is None:
            return {
                "synced": False,
                "message": "No league synced. Run `draftgpt sync league`.",
            }
        league = session.get(League, league_season.league_id)
        owner = (
            session.query(Team)
            .filter(Team.league_id == league.id, Team.is_owner_team.is_(True))
            .first()
        )
        return {
            "synced": True,
            "league": league.name,
            "provider": league.provider_key,
            "season": league_season.season,
            "status": league_season.status,
            "current_week": league_season.current_week,
            "team_count": league_season.team_count,
            "owner_team": owner.name if owner else None,
            "players_known": session.query(Player).count(),
            "projections_stored": session.query(Projection).count(),
            "sources": [
                {
                    "provider": f.provider,
                    "capability": f.capability,
                    "last_success_at": f.last_success_at,
                    "health": f.health,
                }
                for f in collect_freshness(session)
            ],
        }


TOOL_DEFINITIONS = [
    {
        "name": "league_status",
        "description": (
            "What league is loaded, how much data is populated, and how fresh each "
            "source is. Call this first if you are unsure whether data exists."
        ),
        "schema": {"type": "object", "properties": {}},
        "fn": lambda **_: tool_league_status(),
    },
    {
        "name": "prep_league",
        "description": (
            "League settings and what they imply for strategy: scoring format, "
            "positional demand, replacement levels, and ranked findings with the "
            "evidence behind each. Use this before giving any draft advice, because "
            "generic advice is often wrong for a specific configuration."
        ),
        "schema": {"type": "object", "properties": {}},
        "fn": lambda **_: tool_prep_league(),
    },
    {
        "name": "prep_draft_board",
        "description": (
            "The draft board ranked by value over replacement under this league's "
            "scoring, with tiers, tier cliffs, ADP, byes, and positional scarcity. "
            "Optionally estimate who is still available at a given pick number."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "string",
                    "description": "Filter to one position, e.g. RB, WR, TE, QB.",
                },
                "limit": {"type": "integer", "description": "Rows to return (default 30)."},
                "at_pick": {
                    "type": "integer",
                    "description": "Estimate the chance each player survives to this pick.",
                },
                "draft_position": {
                    "type": "integer",
                    "description": "Your slot in the draft order, for snake turn maths.",
                },
                "tier_max": {"type": "integer", "description": "Only show tiers up to this."},
            },
        },
        "fn": lambda **kw: tool_prep_draft_board(**kw),
    },
    {
        "name": "prep_player",
        "description": (
            "One player's value for this league: projected points under your scoring, "
            "value over replacement, positional rank, tier, whether he is the last of "
            "his tier, ADP versus value, bye week, and nearby alternatives. Returns an "
            "'ambiguous_player' error listing candidates rather than guessing between "
            "players with the same name."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Player name to look up."},
                "at_pick": {
                    "type": "integer",
                    "description": "Estimate availability at this pick number.",
                },
            },
            "required": ["name"],
        },
        "fn": lambda **kw: tool_prep_player(**kw),
    },
    {
        "name": "roster_recommend",
        "description": (
            "Optimal starting lineup for a week, with changes from the currently "
            "configured lineup, conditional triggers for questionable players, and "
            "next check time. Requires a drafted roster."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "week": {"type": "integer", "description": "Fantasy week."},
                "objective": {
                    "type": "string",
                    "enum": ["floor", "balanced", "ceiling"],
                    "description": "Risk preference.",
                },
            },
            "required": ["week"],
        },
        "fn": lambda **kw: tool_roster_recommend(**kw),
    },
]


def main() -> None:
    """Run the stdio MCP server.

    Targets the mcp 2.x server API, which configures handlers through
    constructor callbacks rather than decorators.
    """
    try:
        import mcp.types as types
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "The MCP server needs the 'mcp' package: pip install 'draftgpt[mcp]'"
        ) from exc

    import anyio

    async def on_list_tools(_ctx: object, _params: object) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=definition["name"],
                    description=definition["description"],
                    inputSchema=definition["schema"],
                    # Declared at protocol level, not just documented: none of
                    # these tools mutate a fantasy platform.
                    annotations=types.ToolAnnotations(
                        readOnlyHint=True, destructiveHint=False, openWorldHint=False
                    ),
                )
                for definition in TOOL_DEFINITIONS
            ]
        )

    async def on_call_tool(
        _ctx: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        definition = next((d for d in TOOL_DEFINITIONS if d["name"] == params.name), None)
        if definition is None:
            return _error_result(types, f"unknown tool: {params.name}")

        arguments = params.arguments or {}
        try:
            # Tools are synchronous and hit SQLite; run them off the event loop
            # so a slow query cannot stall the MCP transport.
            result = await anyio.to_thread.run_sync(lambda: definition["fn"](**arguments))
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as data
            logger.exception("mcp tool failed: %s", params.name)
            return _error_result(types, str(exc), kind=type(exc).__name__)

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=_json(result))],
            structuredContent=result if isinstance(result, dict) else None,
        )

    server = Server(
        SERVER_NAME,
        version="0.1.0",
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    anyio.run(run)


def _error_result(types: Any, message: str, kind: str = "error") -> Any:
    """Errors are returned as tool content, not transport failures, so the
    assistant can explain the problem to the user instead of silently retrying."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_json({"error": kind, "message": message}))],
        isError=True,
    )


if __name__ == "__main__":
    main()
