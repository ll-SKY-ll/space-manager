"""Validation helpers that verify a room is a plain room and not a (sub)space.

Kept free of any bot/command concerns so the checks are easy to unit test.
"""

from __future__ import annotations

from mautrix.client import Client
from mautrix.errors import MatrixRequestError
from mautrix.types import EventType, RoomID

SPACE_TYPE = "m.space"


class RoomValidationError(Exception):
    """Raised when a room fails one of the plain-room checks.

    The message is safe to relay to the requesting user.
    """


async def ensure_plain_room(client: Client, room_id: RoomID) -> None:
    """Verify that room_id is a normal room and not a space or subspace.

    Performs two checks (the bot must already be joined to the room):
      1. The m.room.create event must not declare "type": "m.space".
      2. The room must not contain any (non-empty) m.space.child state
         events, i.e. it must not act as a space itself.

    Raises RoomValidationError if any check fails.
    """
    await _check_create_event(client, room_id)
    await _check_no_space_children(client, room_id)


async def _check_create_event(client: Client, room_id: RoomID) -> None:
    try:
        create_content = await client.get_state_event(room_id, EventType.ROOM_CREATE)
    except MatrixRequestError as e:
        raise RoomValidationError(
            f"Could not read the m.room.create event of {room_id}: {e.message}"
        ) from e

    # `type` may be absent entirely on plain rooms; handle both attr and
    # serialized-dict access for robustness across mautrix versions.
    room_type = getattr(create_content, "type", None)
    if room_type is None:
        try:
            room_type = create_content.serialize().get("type")
        except AttributeError:
            room_type = None

    if room_type is not None and str(room_type) == SPACE_TYPE:
        raise RoomValidationError(
            f"{room_id} is a **space** (`type: m.space` in m.room.create), "
            f"not a plain room. Refusing to add it."
        )


async def _check_no_space_children(client: Client, room_id: RoomID) -> None:
    try:
        state = await client.get_state(room_id)
    except MatrixRequestError as e:
        raise RoomValidationError(
            f"Could not read the state of {room_id}: {e.message}"
        ) from e

    for evt in state:
        if evt.type != EventType.SPACE_CHILD:
            continue
        # An m.space.child event with empty content is a *removed* child and
        # therefore harmless — only non-empty content counts.
        content = evt.content.serialize() if hasattr(evt.content, "serialize") else evt.content
        if content:
            raise RoomValidationError(
                f"{room_id} contains an `m.space.child` state event "
                f"(child: `{evt.state_key}`), which makes it act like a "
                f"space. Refusing to add it."
            )
