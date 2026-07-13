# space-manager

A [maubot](https://github.com/maubot/maubot) plugin for managing `m.space.child`
state events through a bot. Authorized users can add or remove child rooms from
Matrix spaces with bot commands, sent from a DM or any room the bot is in.

## What it does

- Adds and removes rooms as children of a space via commands.
- Restricts who may edit which space through a config permission map.
- On additions, verifies the target room is a normal room and not a space or
  subspace before adding it.
- Optionally posts a notice to a log room for every action and every failure.

## Requirements

- A running maubot instance.
- A bot account that is joined to each space it manages, with enough power level
  in those spaces to send `m.space.child` state events (by default this needs
  power level 50, but it depends on the space's `m.room.power_levels`).

## Installation

Download the `.mbp` from the latest release

Then upload the resulting `.mbp` file, create an instance, and point it at a
client.

## Configuration

All configuration lives in the instance config. See `base-config.yaml` for the
full annotated defaults. The options are:

### `instance_admins`

A list of user IDs allowed to manage every configured space. Note that this
does not bypass the space list: admins can only edit spaces that appear under
`permissions`. Unconfigured spaces are always rejected.

```yaml
instance_admins:
  - '@admin:example.org'
```

### `permissions`

The per-space permission map. Each entry has:

- `room` — the room ID of the space.
- `vias` — servers written into the `via` field of the `m.space.child` events
  the bot creates. Either a list of server names, or the string `auto` to let
  the bot choose (see below).
- `allowed_editors` — user IDs allowed to add/remove children of this space.

```yaml
permissions:
  - room: '!space:example.org'
    vias:
      - 'example.org'
    allowed_editors:
      - '@someone:example.org'
```

If `vias` is set to `auto`, the bot picks up to three servers from the child
room's membership when a room is added, following the routing recommendation in
the Matrix spec appendices: the server of the highest power level member first,
then the most populous servers. Servers denied by the room's
`m.room.server_acl` and bare IP-literal servers are skipped. The result
reflects the room's membership at the time of the addition and is not updated
afterwards.

### `join_vias`

Servers passed as routing hints when the bot joins a child room to inspect it.
Joining by a bare room ID only works if the bot's homeserver already knows the
room, so for rooms on other servers this needs to list at least one server that
is likely to be in them (your own homeserver name is usually a good default).

```yaml
join_vias:
  - 'example.org'
```

### `notification_room`

Room ID of a room where the bot posts an `m.notice` for every successful
addition/removal, every failed attempt, and rejected invites. The bot must be
a member of this room. Leave as `''` to disable.

```yaml
notification_room: '!log:example.org'
```

### `invites`

Controls which invites the bot accepts. Patterns are globs matched against the
full user ID (`*` matches any sequence, `?` matches one character). The deny
list always overrules the allow list, and a user must match at least one allow
pattern to be accepted — so an empty allow list rejects everyone.

```yaml
invites:
  allow:
    - '@*:example.org'
  deny:
    - '@spammer:example.org'
```

This is only enforced if client-level autojoin is **disabled** for the bot in
the maubot manager. With autojoin enabled, maubot joins invited rooms before
the plugin can act, and this section has no effect.

## Commands

```
!space-manager add <child_room_id> <space_room_id>
!space-manager remove <child_room_id> <space_room_id>
```

Both room arguments must be room IDs (starting with `!`), not aliases.

Commands from users that are neither instance admins nor listed as an allowed
editor of any configured space are ignored entirely: the bot does not reply,
not even with an error. Authorized users trying to edit a space they don't
have access to do get an error reply.

### add

1. Checks the sender is allowed to edit the space.
2. If the room is already a child of the space, does nothing and says so.
3. Joins the child room.
4. Checks the child room is a normal room: its `m.room.create` event must not
   have `type: m.space`, and it must not contain any `m.space.child` state
   events of its own.
5. Sends the `m.space.child` event to the space.
6. Leaves the child room again.

The bot only joins the child room to inspect it and does not stay, regardless
of whether the addition succeeds or fails. The exception is rooms the bot was
already a member of before the command was run — those it never leaves.

### remove

1. Checks the sender is allowed to edit the space.
2. Sends an `m.space.child` event with empty content, which removes the child.

Room validation is not performed for removals.

## Notes

- The bot does not modify `m.space.parent` events on the child room; it only
  writes `m.space.child` on the space.
- The `suggested` and `order` fields on `m.space.child` are not set.
- There is no persistent database; all state lives in the config and in Matrix.

## License

MIT