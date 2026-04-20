# Task bot cheatsheet — VJ & Maggie

Two-minute reference for day-to-day use. Pinned in `#tasks`.

## Slash commands

| Command | What it does |
|---|---|
| `/task` | Open the new-task form (title, property, assignee, priority, due date) |
| `/task <one-liner>` | **Quick create** — parses the sentence and skips the form |
| `/task mine` | Your open tasks (only you see it) |
| `/task list` | All open tasks across both properties + business |
| `/task list palm` | Open tasks for The Palm Club |
| `/task list villa` | Open tasks for Villa Bougainvillea |
| `/task list business` | Business-wide tasks (compliance, admin, etc.) |
| `/task help` | This cheatsheet in Slack |

## Quick create (one-liner)

Type a sentence and the bot figures out the fields. Example:

```
/task maggie fix garbage disposal at palm urgent tomorrow
```

Creates: assignee=Maggie, property=Palm, priority=Urgent, due=tomorrow, title="fix garbage disposal".

Recognized words (in any position, at the start or end of the sentence):

| Kind | Words |
|---|---|
| Assignee | `vj`, `maggie` |
| Property | `palm`, `villa`, `business` / `biz` |
| Priority | `low`, `normal` / `medium`, `high`, `urgent` |
| Due | `today`, `tomorrow`, `monday`…`friday` (next occurrence), `2026-05-01` (ISO date) |

Anything not recognized stays in the title. Metadata words in the **middle** of the sentence are left alone, so "follow up with low-income tenant" keeps "low" as part of the title.

If the parser can't figure something out, it defaults: assignee → you, property → business-wide, priority → normal. The card posts to `#tasks` immediately and you can tweak with the buttons.

## Buttons on a task card

Every task posted to `#tasks` has a row of buttons:

- **✅ Mark done** — close the task. Updates the card + drops a note in the thread.
- **▶️ Start** — flip status to "in progress." Handy when you're actually working on it.
- **⏸ Pause** — go back to "open."
- **🚧 Block** — you're stuck; status becomes "blocked." Reason goes in the thread.
- **🔓 Unblock** — back to open.
- **🔁 Swap assignee** — flip the task to the other person (one click, no dropdown).
- **↩️ Reopen** — closed tasks only.

## Comments

**Reply in the task's thread. That's it.** Every thread reply is saved as a comment on the task. No special syntax.

```
[Maggie] Called the plumber, coming Thursday morning.
[VJ]     Thursday works, house is empty.
```

## Priority colors

| Emoji | Priority | When to use |
|---|---|---|
| 🔴 | Urgent | Guest is there and something is broken |
| 🟠 | High | Time-sensitive but not on-fire |
| 🟢 | Normal | Most things (default) |
| ⚪ | Low | Whenever-you-get-to-it |

## Daily digest (9am Phoenix)

Every morning you'll get a DM from the bot with:
1. Your **overdue** tasks (with days-late)
2. Your tasks **due today**

If anything is more than **3 days overdue**, it also gets posted to `#tasks` with both your names attached. Not to shame — to make sure it's not invisible.

## Business-wide tasks

Tasks that don't belong to a specific property go to `🏢 Business-wide`:

- Scottsdale STR registration, renewals, compliance
- Insurance, taxes, legal
- Marketing decisions that cover both listings
- Software / tooling

## Tips

- **Short titles are better.** "Fix disposal at Palm" beats "The garbage disposal at The Palm Club is not working and the guest reported it last night at about 11pm."
- **Put detail in the description field**, not the title. The title is the scannable line in lists.
- **Due dates are optional but recommended.** Without one, a task never shows up in your morning digest.
- **Swap assignee if you pick it up.** That way the other person's `/task mine` doesn't still show it.
- **Use the thread for back-and-forth.** It's the full history of what happened on a task — and it's searchable in Slack.
- **Don't create duplicate tasks.** Search `#tasks` first (Slack's search works on task titles since they're in the card header).

## Common flows

**Guest reported something broken:**
1. `/task` → title, pick property, assign, Urgent or High, submit.
2. When fixed, click ✅ Mark done.

**Something Maggie should do:**
1. `/task` → pick her as assignee. She'll get a DM.

**You start working on one of your tasks:**
1. Click ▶️ Start. Now the other person knows it's live.

**You're stuck on something:**
1. Click 🚧 Block. Reply in thread explaining what you need.

**Weekly review:**
1. `/task list` in your own DM with the bot to see everything open.

## Troubleshooting

- **"I don't have you in the users list yet"** → VJ needs to edit `seed/users.json` with your Slack member ID and run `./setup-tasks.sh`.
- **Slash command returns an error** → Slack will show the error. If it says something about the app not responding, the Lambda might be cold-starting; try again.
- **Buttons don't seem to work** → Check `#bot-errors` (if configured). Otherwise ping VJ.
- **Accidentally closed the wrong task** → Click ↩️ Reopen on the card. The action is reversible.

## Not supported yet (maybe later)

- Recurring tasks (e.g. weekly restock). Today you create a new one each time.
- File attachments (guest photo of the thing).
- Tag filtering (`/task list tag:compliance`).
- Editing a task after creation (beyond status/assignee). Closest workaround: close it and create a new one with corrections.
- Home tab dashboard. Use `/task mine` and `/task list` for now.

## Where this lives

- Tasks live in S3 (`s3://hospitality-hq-tasks/tasks/`). One JSON object per task, versioned.
- Static config (properties + users) is in `s3://hospitality-hq-tasks/config/`.
- Code is in `src/task_*.py`. SAM template has the AWS resources.
- Design discussion history is in the PR description for the initial rollout.
- Slack Events (thread replies) share one URL with the guest-alerts workflow (`/slack/events`); `thread_handler.py` dispatches task-channel events to `task_handler.handle_task_thread_message`.
