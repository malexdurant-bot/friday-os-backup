# Private Obsidian knowledge connector

Friday can retrieve reference excerpts from the private GitHub vault
`malexdurant-bot/marcus-business-brain`. The connector is **disabled by default**.
It adds reference text, not model-callable tools or autonomous actions. Existing
Friday policy, one-agent registry, spend controls and Slack allowlists remain.

## Data flow and privacy

An operator explicitly runs `scripts/sync-obsidian.py`. It reads `main`, pins the
commit and its tree, and downloads eligible Markdown blobs by SHA from GitHub's
HTTPS API. It verifies blob hashes and atomically replaces a mode-600 JSON
snapshot. The token is never stored in the snapshot or settings. Refresh uses
GET only, preserves inherited proxy/CA configuration, refuses redirects and has
no retry or scheduling loop. A failure preserves the prior snapshot.

Only UTF-8 Markdown regular files are included. Hidden directories/files,
symlinks, executable files, attachments and other formats are excluded. Bounds:
4,000 notes, 256 KiB/note, 24 MiB aggregate text, 32 MiB encoded snapshot. Truncated
trees, oversized eligible notes and invalid blobs fail the refresh rather than
silently publishing a partial corpus. A complete refresh removes deleted notes.
The snapshot includes all eligible Markdown, including README/template notes;
there is no folder-level relevance or confidentiality classification.

When explicitly enabled, each Friday invocation performs local lexical retrieval
(up to three 1,200-character windows), with commit-pinned GitHub source links and
line references. Excerpts fit within the existing 8,000-character task bound,
with a maximum 4,600-character reference block. Missing/corrupt snapshots or
insufficient context budget stop the invocation. No matches are labelled as no
evidence. No embeddings service or additional database schema is used.

**Enabled excerpts are sent to the configured OpenRouter model and may appear in
its answer and the existing conversation/Slack storage.** They are not kept
local-only once enabled. Protect the snapshot and any storage/replies accordingly.
GitHub source links retain the private repository's normal access requirements.
Note text is labelled as untrusted reference data and JSON quoted. This helps
prompt interpretation; it cannot guarantee resistance to every prompt injection.
The enforced absence of tools/delegation remains the action boundary.

## Refresh locally

Supply `OBSIDIAN_GITHUB_TOKEN` securely to the refresh process with read-only
Contents permission for this repository. For the managed development environment,
the existing `GH_TOKEN` is used only when the dedicated variable is absent.
Never paste tokens into chat, put them in shell arguments, or commit them.
A hosted worker needs its own securely configured credential if it will refresh.
The ChatGPT GitHub connector's access does not provision hosted credentials.

From `/workspace/friday-os`:

```sh
.venv-runtime/bin/python scripts/sync-obsidian.py
```

The default private snapshot is `.friday-local/obsidian-snapshot.json` next to the
source root. `OBSIDIAN_SNAPSHOT_PATH` can select an absolute private path. Refresh
prints only counts, bytes, revision and time; it never prints notes or tokens.
The snapshot is ignored by Git and excluded from source/container deliverables.
Do not place it in a public directory, source archive or Docker image.

## Enable, verify, and roll back

After reviewing data flow and choosing an appropriate host, set
`OBSIDIAN_ENABLED=true` and `OBSIDIAN_SNAPSHOT_PATH` on the specific Friday process.
No setting was persisted by this implementation. Local snapshot retrieval has
been checked without a model call; live answer quality/Slack citations and hosted
storage have not been accepted. Use the existing authorized acceptance workflow
before deployment. For hosted use, provision private persistent storage and run
a manual refresh into that storage; the existing image does not ship private
notes or include the operator script. Do not enable a hosted worker until its
snapshot is available.

Disable by unsetting `OBSIDIAN_ENABLED` or setting it to `false`, then restart.
There are no database migrations to reverse. Source checkpoint before this work:
`/workspace/work/obsidian-resume/source-before.tar.gz`, with matching file hashes
in `baseline-sha256.json`. Restore only the changed source files if needed; do
not overwrite `.friday-local`, dependencies, database volumes or newer work.

## Validation and limitations

See `verification/obsidian-connector.json` for real snapshot metadata and
`TEST-RESULTS.md` for offline checks. Search is lexical, not semantic; synonyms,
ambiguous queries and old conversation text may affect ranking. Snapshot age and
revision are presented to the model, but no automatic expiry or live refresh is
performed. Removed remote notes remain locally available until the next explicit
successful refresh. Existing older milestone statements remain historical;
read the latest handoff sections for current status.
