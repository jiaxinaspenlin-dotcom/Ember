# Tasks

Work with a named owner and a clear next step.

## Statuses

`To do` → `In progress` → `Blocked` → `Done`

Every status is reachable from every other, because real work moves backwards
too: a "done" task reopens, an "in progress" task gets blocked. The only
bookkeeping rule is that `completed_at` is set when a task becomes `Done` and
cleared when it leaves — enforced in the service *and* by a database CHECK
(`done` requires `completed_at`).

## Priorities

`Low` · `Normal` · `High` · `Urgent`

## Permissions

| Action | Creator | Assignee | Admin | Other member |
| --- | --- | --- | --- | --- |
| Create | ✅ | ✅ | ✅ | ✅ |
| Read | ✅ | ✅ | ✅ | ✅ |
| Edit title/description/priority/due | ✅ | ❌ | ✅ | ❌ |
| Assign / reassign | ✅ | ❌ | ✅ | ❌ |
| Change status | ✅ | ✅ | ✅ | ❌ |

The split matches how teams actually work: **the creator and admins retain
management control** (what the task is, who owns it), while **the assignee owns
its progress** (where it stands). An assignee cannot hand their task to someone
else; a bystander cannot mark it done.

`permissions.require_task_management()` and
`permissions.require_task_status_update()` are the enforcement points, called by
the service — not by the route, and certainly not by the template.

## Fields

`id` · `title` · `description` · `creator_id` · `assignee_id` ·
`source_message_id` · `source_channel_id` · `status` · `priority` · `due_at` ·
`completed_at` · timestamps

Tasks may be unassigned. `assignee_id` is `ON DELETE SET NULL`, so removing an
account never destroys the work item.

## Creating one

1. **From a message** — "Turn into… → Task" on a public channel message. The
   description is pre-filled and `source_message_id` / `source_channel_id` are
   preserved, so the task detail page links back to the conversation.
2. **Directly** — the "Create a task" form on `/tasks`.

## Assignment and notifications

| Event | Notified |
| --- | --- |
| Created with an assignee | the assignee |
| Reassigned | the new assignee |
| Status changed | the creator and the assignee, minus whoever acted |

Self-notifications are always skipped.

## Filters

`/tasks` supports status, priority, assignee, creator, **assigned to me**,
**created by me**, **unassigned**, and free-text search over title and
description — all applied in SQL, paginated 20 per page.

The home dashboard shows your five most urgent open tasks
(`due_at ASC NULLS LAST`), and `/tasks?assigned_to_me=true` is one click away.

## Changing status

Two paths, same service call:

- **Inline** — the `<select>` on any task row posts to
  `/hx/tasks/{id}/status` and swaps just that row (HTMX).
- **Detail page** — a row of buttons at `/tasks/{id}`.

Both call `tasks.update_task_status()`, which checks the permission, applies the
completion timestamp rule, notifies, and writes an audit event.

## Empty states

- No tasks at all: "No tasks yet." with a prompt to create one or turn a message
  into one.
- Filtered to yours with none assigned: "No tasks are assigned to you."

## Tests

`app/tests/test_actions.py` covers creation from a message, assignee
notification, the `completed_at` set/clear rule, reassignment and unassignment,
every filter, the open-task count, and title validation.
`app/tests/test_permissions.py` asserts that a stranger cannot change status and
that an assignee cannot reassign. `test_web_flows.py` drives the inline HTMX
status change.
