# Automatic, current Business Brain access

Friday already retrieves business evidence automatically; the user does not
need to mention Obsidian, searching, or the Business Brain. Live mode additionally
checks the private GitHub vault's `main` branch before every model invocation.
It scans all eligible Markdown notes, retrieving relevant excerpts within the
existing input limit. It does not send the entire corpus to the model each time.

## Runtime setup

Configure these on the existing Render friday-worker:

- `OBSIDIAN_ENABLED=true`
- `OBSIDIAN_LIVE=true`
- `OBSIDIAN_GITHUB_TOKEN`: a dedicated fine-grained GitHub token restricted to
  `malexdurant-bot/marcus-business-brain`, with Contents: Read-only. Supply through
  Render Environment settings, never chat, Git, logs or command arguments.
- `OBSIDIAN_SNAPSHOT_PATH=/tmp/friday-brain/obsidian-snapshot.json`
- `OBSIDIAN_SEED_PATH=/etc/secrets/obsidian-snapshot.json`

The existing Render secret file is a bootstrap cache only. The live cache is a
private, writable runtime file; a restart reconstructs it from GitHub, reusing
unchanged seed blobs. No new paid service, background scheduler, webhook, write
permission, or database migration is required. Do not deploy live mode until the
dedicated credential has been configured and verified. The development GH_TOKEN
is not copied to Render.

## Freshness and privacy

At startup and on each owner question, Friday verifies `main` with GitHub, pins
its revision, and downloads only new/changed Markdown blobs. Deleted notes are
removed when the complete new snapshot is published. Unchanged revisions avoid
blob downloads. Publishing uses atomic replacement and mode 600. The snapshot
and token remain outside source and Docker layers. The entire Markdown vault
is searchable; attachments, binaries and hidden configuration remain excluded.
The existing note/file/total-size limits still apply.

This is live against the latest committed version in GitHub. Local Obsidian
edits must finish syncing to GitHub first. A commit made during an answer is
picked up on the next question. There is no stale-cache fallback: a failed
refresh returns a clear freshness-unavailable message without reserving model
budget or calling the model. No partial snapshot is used. GitHub outages, token
expiry, read permission changes or oversized vaults can prevent business answers
until resolved. No customer/CRM/payment capability is added.

The model receives bounded, quoted relevant passages with commit-pinned source
links and freshness metadata. Excerpts are reference data, not instructions.
Private excerpts may appear in the existing OpenRouter call, stored conversation
and Slack reply. Slack access remains limited to the configured owner/channel.

## Verification and rollback

Tests cover automatic clean-question retrieval, checking GitHub on every call,
unchanged-cache reuse, writable cache from a read-only seed, added/changed/deleted
notes, refusal on refresh failure, and refresh before model-budget reservation.
Live GitHub smoke checks can use the development credential locally without
copying it to the host. Final hosted acceptance requires checking the deployed
worker with the dedicated token.

Rollback to the previously accepted fixed snapshot: set `OBSIDIAN_LIVE=false`
and `OBSIDIAN_SNAPSHOT_PATH=/etc/secrets/obsidian-snapshot.json`, then redeploy.
Keep the existing database, ledger and Slack credentials. Disable all knowledge
access separately with `OBSIDIAN_ENABLED=false` if needed.
