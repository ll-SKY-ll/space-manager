"""Configuration schema and permission resolution for the space manager bot."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import List, Optional

from attr import dataclass
from mautrix.types import RoomID, UserID
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper


@dataclass
class SpacePermission:
    """Permission entry for a single managed space."""

    room: RoomID
    vias: List[str]
    vias_auto: bool
    allowed_editors: List[UserID]

    @classmethod
    def from_dict(cls, raw: dict) -> "SpacePermission":
        raw_vias = raw.get("vias")
        vias_auto = isinstance(raw_vias, str) and raw_vias.strip().lower() == "auto"
        return cls(
            room=RoomID(raw.get("room", "")),
            vias=[] if vias_auto else list(raw_vias or []),
            vias_auto=vias_auto,
            allowed_editors=[UserID(u) for u in (raw.get("allowed_editors") or [])],
        )


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("invites")
        helper.copy("notification_room")
        helper.copy("join_vias")
        helper.copy("instance_admins")
        helper.copy("permissions")

    # -- Accessors -----------------------------------------------------------

    @property
    def join_vias(self) -> List[str]:
        return list(self["join_vias"] or [])

    @property
    def notification_room(self) -> Optional[RoomID]:
        room = self["notification_room"]
        return RoomID(room) if room else None

    @property
    def invite_allow(self) -> List[str]:
        return list((self["invites"] or {}).get("allow") or [])

    @property
    def invite_deny(self) -> List[str]:
        return list((self["invites"] or {}).get("deny") or [])

    @property
    def instance_admins(self) -> List[UserID]:
        return [UserID(u) for u in (self["instance_admins"] or [])]

    @property
    def spaces(self) -> List[SpacePermission]:
        return [SpacePermission.from_dict(entry) for entry in (self["permissions"] or [])]

    def get_space(self, space_id: RoomID) -> Optional[SpacePermission]:
        """Return the permission entry for a space, or None if unconfigured."""
        for space in self.spaces:
            if space.room == space_id:
                return space
        return None

    # -- Permission checks ---------------------------------------------------

    def invite_allowed(self, user_id: UserID) -> bool:
        """Whether an invite from user_id should be accepted.

        Semantics mirror m.room.server_acl, applied to user IDs:
          1. If the user matches ANY deny pattern, the invite is rejected —
             the deny list always overrules the allow list.
          2. Otherwise the user must match at least one allow pattern.
             An empty allow list rejects everyone.

        Patterns are globs matched case-sensitively against the full MXID
        (`*` = any sequence, `?` = one character).
        """
        if any(fnmatchcase(user_id, pattern) for pattern in self.invite_deny):
            return False
        return any(fnmatchcase(user_id, pattern) for pattern in self.invite_allow)

    def is_instance_admin(self, user_id: UserID) -> bool:
        return user_id in self.instance_admins

    def is_known_user(self, user_id: UserID) -> bool:
        """Whether user_id is authorized for *anything* on this instance:
        an instance admin, or an allowed editor of at least one space.

        Users failing this check should get no reaction from the bot at all.
        """
        if self.is_instance_admin(user_id):
            return True
        return any(user_id in space.allowed_editors for space in self.spaces)

    def can_edit(self, user_id: UserID, space_id: RoomID) -> bool:
        """Whether user_id may add/remove children of space_id via the bot.

        Rules:
          * The space must be configured — otherwise nobody may edit it.
          * Instance admins may edit every configured space.
          * Otherwise the user must be in the space's allowed_editors list.
        """
        space = self.get_space(space_id)
        if space is None:
            return False
        if self.is_instance_admin(user_id):
            return True
        return user_id in space.allowed_editors