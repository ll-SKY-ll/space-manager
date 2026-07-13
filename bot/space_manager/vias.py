"""Automatic `via` server selection for m.space.child events.

Implements the routing recommendation from the Matrix spec appendices:
pick up to 3 unique servers likely to remain in the room long-term:
  1. the server of the highest power level (>= 50) joined user,
  2. then servers by descending joined-member count.
Servers denied by the room's m.room.server_acl and bare IP-literal
servers are never chosen.

The ranking itself is a pure function (rank_servers) so it can be unit
tested without a Matrix client.
"""

from __future__ import annotations

import ipaddress
from collections import Counter
from fnmatch import fnmatchcase
from typing import Dict, List, Optional

from mautrix.client import Client
from mautrix.errors import MatrixRequestError, MNotFound
from mautrix.types import EventType, RoomID, UserID

MAX_VIAS = 3
MIN_PL_FOR_FIRST_CANDIDATE = 50

# mautrix's EventType has no predefined constant for m.room.server_acl,
# so construct it by name (registered as a state event type).
SERVER_ACL_EVENT = EventType.find("m.room.server_acl", EventType.Class.STATE)


def _server_of(user_id: str) -> Optional[str]:
    _, _, server = user_id.partition(":")
    return server or None


def is_ip_literal(server: str) -> bool:
    """Whether a server name is a bare IP address (optionally with port)."""
    host = server
    if host.startswith("["):  # [IPv6]:port or [IPv6]
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:  # host:port
        host = host.split(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _acl_allows(server: str, acl: Optional[dict]) -> bool:
    """Evaluate a server against an m.room.server_acl (deny overrules allow)."""
    if not acl:
        return True
    if any(fnmatchcase(server, p) for p in acl.get("deny") or []):
        return False
    allow = acl.get("allow")
    if allow is None:  # no allow list present -> everything not denied is fine
        return True
    return any(fnmatchcase(server, p) for p in allow)


def rank_servers(
    joined_users: List[str],
    power_levels: Dict[str, int],
    server_acl: Optional[dict] = None,
    limit: int = MAX_VIAS,
) -> List[str]:
    """Rank candidate via servers per the spec's routing recommendation."""
    def eligible(server: Optional[str]) -> bool:
        return (
            bool(server)
            and "." in server  # real server names contain at least one dot
            and not is_ip_literal(server)
            and _acl_allows(server, server_acl)
        )

    result: List[str] = []

    # 1. Server of the highest-PL joined user with PL >= 50.
    ranked_pl_users = sorted(
        (u for u in joined_users if power_levels.get(u, 0) >= MIN_PL_FOR_FIRST_CANDIDATE),
        key=lambda u: power_levels.get(u, 0),
        reverse=True,
    )
    for user in ranked_pl_users:
        server = _server_of(user)
        if eligible(server):
            result.append(server)
            break

    # 2. Fill the rest by descending joined-member count.
    counts = Counter(s for s in map(_server_of, joined_users) if s)
    for server, _ in counts.most_common():
        if len(result) >= limit:
            break
        if server not in result and eligible(server):
            result.append(server)

    return result


async def pick_vias(client: Client, room_id: RoomID, limit: int = MAX_VIAS) -> List[str]:
    """Compute via servers for room_id. The client must be joined to it."""
    members = await client.get_joined_members(room_id)
    joined_users = [str(UserID(u)) for u in members.keys()]

    power_levels: Dict[str, int] = {}
    try:
        pl_content = await client.get_state_event(room_id, EventType.ROOM_POWER_LEVELS)
        raw = pl_content.serialize() if hasattr(pl_content, "serialize") else pl_content
        power_levels = {str(u): int(pl) for u, pl in (raw.get("users") or {}).items()}
    except (MNotFound, MatrixRequestError):
        pass  # No/unreadable power levels -> rank purely by population.

    server_acl: Optional[dict] = None
    try:
        acl_content = await client.get_state_event(room_id, SERVER_ACL_EVENT)
        server_acl = acl_content.serialize() if hasattr(acl_content, "serialize") else acl_content
    except (MNotFound, MatrixRequestError):
        pass  # No ACL in the room.

    return rank_servers(joined_users, power_levels, server_acl, limit)