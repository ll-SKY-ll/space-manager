"""Space Manager — a maubot plugin for delegated m.space.child management.

Workflow:
  * The bot accepts/rejects invites based on configured allow/deny glob
    lists (deny always overrules allow).
  * `!space-manager add <child_room_id> <space_room_id>` adds a child to a
    space, but only after verifying that:
      1. the sender may edit the target space (config permission map),
      2. the child room is a plain room — not a space and not acting as one.
  * `!space-manager remove <child_room_id> <space_room_id>` removes a child
    (permission check only; no room-type validation for removals).
  * Every successful action and every failed attempt is reported as an
    m.notice message to the configured notification room (if any).
"""

from __future__ import annotations

from typing import Type

from maubot import MessageEvent, Plugin
from maubot.handlers import command, event
from mautrix.errors import MatrixRequestError, MNotFound
from mautrix.types import (
    EventType,
    Membership,
    RoomID,
    StrippedStateEvent,
)
from mautrix.util.config import BaseProxyConfig

from .config import Config
from .validation import RoomValidationError, ensure_plain_room
from .vias import pick_vias


class SpaceManagerBot(Plugin):
    config: Config

    async def start(self) -> None:
        await super().start()
        self.config.load_and_update()

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    # -- Invite handling -----------------------------------------------------

    @event.on(EventType.ROOM_MEMBER)
    async def handle_invite(self, evt: StrippedStateEvent) -> None:
        """Accept or reject invites according to the configured allow/deny
        glob lists (deny always overrules allow).

        Note: for this to be authoritative, client-level autojoin must be
        DISABLED for this bot in the maubot manager — otherwise maubot core
        joins before this handler can decide.
        """
        if (
            evt.state_key != self.client.mxid
            or evt.content.membership != Membership.INVITE
        ):
            return

        if self.config.invite_allowed(evt.sender):
            self.log.info(f"Accepting invite to {evt.room_id} from {evt.sender}")
            await self.client.join_room(evt.room_id)
        else:
            self.log.info(f"Rejecting invite to {evt.room_id} from {evt.sender}")
            try:
                # Leaving while in the "invite" state is the spec-defined way
                # to reject an invite; there is no separate reject API.
                await self.client.leave_room(evt.room_id)
            except Exception as e:
                # Rejecting a federated invite requires the homeserver to
                # reach out to the remote server — this can genuinely fail,
                # so surface it instead of pretending we rejected.
                self.log.exception(f"Failed to reject invite to {evt.room_id}")
                await self._notify(
                    f"⚠️ FAILED to reject an invite to {evt.room_id} from "
                    f"{evt.sender}: {e} — the invite may still be pending."
                )
                return
            self.log.info(f"Rejected invite to {evt.room_id}")
            await self._notify(
                f"🚫 Rejected an invite to {evt.room_id} from {evt.sender} "
                f"(invite policy)"
            )

    # -- Commands ------------------------------------------------------------

    @command.new(name="space-manager", require_subcommand=False)
    async def space_manager(self, evt: MessageEvent) -> None:
        """Runs when no known subcommand matched (bare `!space-manager` or an
        unknown subcommand).

        require_subcommand must be False: with True, maubot itself replies
        with an auto-generated usage message before our handlers run, which
        would allow unauthorized users to trigger a interaction with the bot.
        """
        if not self.config.is_known_user(evt.sender):
            # Completely silent for unauthorized users — no reply, no error.
            self.log.debug(f"Silently ignoring bare command from {evt.sender}")
            return
        await evt.reply(
            "Usage:\n"
            "* `!space-manager add <child_room_id> <space_room_id>`\n"
            "* `!space-manager remove <child_room_id> <space_room_id>`"
        )

    @space_manager.subcommand("add", help="Add a room as a child of a space.")
    @command.argument("child_room_id")
    @command.argument("space_room_id")
    async def add(self, evt: MessageEvent, child_room_id: str, space_room_id: str) -> None:
        if not self.config.is_known_user(evt.sender):
            # Completely silent for unauthorized users — no reply, no error.
            self.log.debug(f"Silently ignoring add command from {evt.sender}")
            return

        child_id, space_id = RoomID(child_room_id), RoomID(space_room_id)
        action = "add"

        reason = await self._validate_request(evt, child_id, space_id)
        if reason:
            await self._fail(evt, action, child_id, space_id, reason)
            return

        # Short-circuit if the room is already a child of the space — no
        # point re-sending an identical state event (checked before joining
        # the child room, so duplicates don't even trigger a join).
        if await self._is_existing_child(space_id, child_id):
            await self._skip(evt, child_id, space_id)
            return

        # Remember whether we were already a member of the child room before
        # this request — if so, we must NOT leave it on failure (it could be
        # the notification room, a command room, etc.).
        was_member = await self._is_joined(child_id)

        # Join the child room so we can inspect its state. Bare room IDs
        # don't route over federation, so pass the configured join_vias as
        # routing hints.
        try:
            await self.client.join_room(child_id, servers=self.config.join_vias or None)
        except MatrixRequestError as e:
            await self._fail(
                evt, action, child_id, space_id,
                f"could not join `{child_id}` to inspect it: {e.message}",
            )
            return

        # Verify the child is a plain room, not a space / subspace.
        try:
            await ensure_plain_room(self.client, child_id)
        except RoomValidationError as e:
            await self._leave_transient(child_id, was_member)
            await self._fail(evt, action, child_id, space_id, f"validation failed: {e}")
            return

        # All checks passed — write the m.space.child event.
        space_config = self.config.get_space(space_id)
        if space_config.vias_auto:
            # Compute vias from the child room per the spec's routing
            # recommendation (we are joined to it at this point).
            vias = await pick_vias(self.client, child_id)
            if not vias:
                await self._leave_transient(child_id, was_member)
                await self._fail(
                    evt, action, child_id, space_id,
                    f"could not determine any suitable via servers for `{child_id}`",
                )
                return
        else:
            vias = space_config.vias
            if space_config.invalid_vias or not vias:
                await self._leave_transient(child_id, was_member)
                detail = (
                    f"invalid via server names in config: "
                    f"{', '.join(space_config.invalid_vias)}"
                    if space_config.invalid_vias
                    else "no via servers configured"
                )
                await self._fail(
                    evt, action, child_id, space_id,
                    f"{detail} — server names must contain a dot, "
                    f"or set `vias: auto`",
                )
                return
        content = {"via": vias}
        try:
            await self.client.send_state_event(
                space_id, EventType.SPACE_CHILD, content, state_key=child_id
            )
        except MatrixRequestError as e:
            await self._leave_transient(child_id, was_member)
            await self._fail(
                evt, action, child_id, space_id,
                f"failed to update `{space_id}`: {e.message}",
            )
            return

        # Leave the child room again — the bot only joined it to inspect it,
        # and staying would let random room members try to interact with it.
        await self._leave_transient(child_id, was_member)

        await self._succeed(evt, action, child_id, space_id)

    @space_manager.subcommand("remove", help="Remove a child room from a space.")
    @command.argument("child_room_id")
    @command.argument("space_room_id")
    async def remove(self, evt: MessageEvent, child_room_id: str, space_room_id: str) -> None:
        if not self.config.is_known_user(evt.sender):
            # Completely silent for unauthorized users — no reply, no error.
            self.log.debug(f"Silently ignoring remove command from {evt.sender}")
            return

        child_id, space_id = RoomID(child_room_id), RoomID(space_room_id)
        action = "remove"

        reason = await self._validate_request(evt, child_id, space_id)
        if reason:
            await self._fail(evt, action, child_id, space_id, reason)
            return

        # Per spec, a child is removed by sending an m.space.child event with
        # empty content for the same state key. No room-type checks here.
        try:
            await self.client.send_state_event(
                space_id, EventType.SPACE_CHILD, {}, state_key=child_id
            )
        except MatrixRequestError as e:
            await self._fail(
                evt, action, child_id, space_id,
                f"failed to update `{space_id}`: {e.message}",
            )
            return

        await self._succeed(evt, action, child_id, space_id)

    # -- Shared checks -------------------------------------------------------

    async def _is_joined(self, room_id: RoomID) -> bool:
        """Whether the bot is currently a joined member of room_id."""
        try:
            return room_id in await self.client.get_joined_rooms()
        except MatrixRequestError as e:
            # If we can't tell, assume we were a member — the safe direction,
            # since it only means we might stay in a junk room, rather than
            # accidentally leaving a room we belong in.
            self.log.warning(f"Could not check membership of {room_id}: {e}")
            return True

    async def _leave_transient(self, room_id: RoomID, was_member: bool) -> None:
        """Leave a room the bot only joined to inspect during an add request
        (whether the add succeeded or failed).

        Never raises, and never leaves rooms the bot was already in before
        the request started.
        """
        if was_member:
            return
        try:
            await self.client.leave_room(room_id)
            self.log.info(f"Left {room_id} again after inspection")
        except Exception:
            self.log.exception(f"Failed to leave {room_id} after inspection")

    async def _is_existing_child(self, space_id: RoomID, child_id: RoomID) -> bool:
        """Whether the space already has a live m.space.child for child_id.

        Per spec, a child event without a valid (non-empty) `via` is not a
        child — that covers both removed children (empty content) and typed
        deserialization artifacts where default fields like `suggested`
        survive serialization even though the event content was empty.
        """
        try:
            content = await self.client.get_state_event(
                space_id, EventType.SPACE_CHILD, state_key=child_id
            )
        except MNotFound:
            return False
        except MatrixRequestError as e:
            # Can't read the space's state — err on the side of proceeding;
            # the actual send will produce a proper error if something is off.
            self.log.warning(f"Could not check existing children of {space_id}: {e}")
            return False
        serialized = content.serialize() if hasattr(content, "serialize") else content
        return bool(serialized.get("via"))

    async def _validate_request(
        self, evt: MessageEvent, child_id: RoomID, space_id: RoomID
    ) -> str | None:
        """Argument sanity + permission check.

        Returns None if the request may proceed, or a human-readable failure
        reason otherwise. Replying/notifying is left to the caller so that
        every outcome flows through _fail()/_succeed().
        """
        for label, room_id in (("child room", child_id), ("space", space_id)):
            if not str(room_id).startswith("!"):
                return f"`{room_id}` is not a valid {label} ID (room IDs start with `!`)"

        if self.config.get_space(space_id) is None:
            return f"the space `{space_id}` is not managed by this bot"

        if not self.config.can_edit(evt.sender, space_id):
            return f"{evt.sender} is not allowed to edit `{space_id}`"

        return None

    # -- Outcome reporting ---------------------------------------------------

    _PAST_TENSE = {"add": "added", "remove": "removed"}

    async def _skip(
        self, evt: MessageEvent, child_id: RoomID, space_id: RoomID
    ) -> None:
        self.log.info(f"{evt.sender}: add {child_id} to {space_id}: already a child, skipped")
        await evt.reply(
            f"ℹ️ `{child_id}` is already a child of `{space_id}` — nothing to do."
        )
        await self._notify(
            f"ℹ️ {evt.sender} tried to add {child_id} to {space_id}, "
            f"but it is already a child (skipped)"
        )

    async def _succeed(
        self, evt: MessageEvent, action: str, child_id: RoomID, space_id: RoomID
    ) -> None:
        preposition = "to" if action == "add" else "from"
        verb = self._PAST_TENSE[action]
        self.log.info(f"{evt.sender}: {action} {child_id} {preposition} {space_id}: ok")
        await evt.reply(f"✅ {verb.capitalize()} `{child_id}` {preposition} `{space_id}`.")
        await self._notify(
            f"✅ {evt.sender} {verb} {child_id} {preposition} {space_id}"
        )

    async def _fail(
        self,
        evt: MessageEvent,
        action: str,
        child_id: RoomID,
        space_id: RoomID,
        reason: str,
    ) -> None:
        self.log.warning(
            f"{evt.sender}: {action} {child_id} / {space_id} failed: {reason}"
        )
        await evt.reply(f"❌ Request failed: {reason}.")
        await self._notify(
            f"⚠️ FAILED {action} by {evt.sender} "
            f"(child: {child_id}, space: {space_id}): {reason}"
        )

    async def _notify(self, text: str) -> None:
        """Post an m.notice to the configured notification room, if any.

        Never raises — a broken notification room must not break commands.
        """
        room = self.config.notification_room
        if not room:
            return
        try:
            await self.client.send_notice(room, text)
        except Exception:
            self.log.exception(f"Failed to send notification to {room}")