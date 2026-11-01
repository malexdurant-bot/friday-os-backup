# Business Brain on the existing Render worker

Service: friday-worker (`srv-dbjg4k67bikc73fbvct0`)
Repository: `malexdurant-bot/friday-os-backup`
Branch: `friday-render-m6`
Prior deployed commit: `a4a68b142ef29ab4b3852754f9efc7ddb5b3bdfb`

The existing worker was discovered live on November 1 after a Slack mention
received an answer without vault knowledge. This deployment was absent from the
recovered local handoff. A temporary local Socket worker was stopped before
updating the hosted worker. Preserve the existing hosted database and checkpoint
marker; never restore a database over it.

The source adds the read-only connector, prompt guidance, startup snapshot
validation and sanitized delivery/usage logs. No credentials or private notes
are in source. The Docker image and hosted restore logic are preserved.

Runtime configuration:

- `OBSIDIAN_ENABLED=true`
- `OBSIDIAN_SNAPSHOT_PATH=/etc/secrets/obsidian-snapshot.json`
- Render secret file `obsidian-snapshot.json`: complete validated private snapshot.

The snapshot is explicitly uploaded over the authenticated Render API. Render's
secret-file storage persists it across deployments. There is no GitHub credential
on the worker and no automatic refresh; repeat the explicit local refresh and
secret-file upload when notes change, then deploy. Do not upload notes to Git,
container layers, application logs or public storage. Excerpts go to the existing
OpenRouter model when the owner invokes Friday and may appear in stored replies.

Expected logs: `Business Brain ready: notes=70 revision=...`, followed by
`Slack Socket Mode connected`. After an owner mention, `Friday usage recorded`
and `Friday reply delivered` record usage and citation count without note text.
A live delivery receipt proves Slack accepted a reply; inspect the owner's
actual reply to assess factual accuracy and citation relevance.

Rollback: set `OBSIDIAN_ENABLED=false`, deploy the prior commit, and retain the
database. The unused snapshot secret file can be removed separately. No database
migration, model change or budget increase is part of this update.

Render API references:
https://api-docs.render.com/reference/add-or-update-secret-file
https://api-docs.render.com/reference/update-env-var
https://api-docs.render.com/reference/create-deploy
