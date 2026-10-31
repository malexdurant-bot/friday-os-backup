# Friday OS recovery backup

Recovery bundle: [friday-os-resume-milestone-5.zip](recovery/friday-os-resume-milestone-5.zip). Download using the file page's **Download raw file** button, or clone this private repository in the new execution environment. No chat download is required.

Verify the ZIP against `SHA256SUMS.txt` before extracting. The ZIP includes separate source and synthetic database archives, internal checksums, and READ-FIRST.md. Preserve existing projects and database volumes. This repository is a recovery backup, not a deployment configuration.

# Read before resuming Friday OS

Resume Milestone 5, already authorized. Milestones 1–4 passed. Source checkpoint commit: 8c41c3d. The Slack app has already been created and installed with app_mentions:read and chat:write. Do not create a duplicate app.

FIRST verify the newly published environment exposes SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET without displaying values. Prior task restarted to revision 2 but retained its original configuration version and had neither Slack binding. Publishing in an environment-editor chat did not update that task. If bindings remain missing, stop and report the exact readiness evidence; do not rebuild or repeat UI instructions known not to work.

HTTP secret presence alone is insufficient for SLACK_SIGNING_SECRET: local HMAC verification needs a usable signing secret, not merely an outbound proxy placeholder. Verify the supported secret delivery mechanism; never print secrets or request them in chat. Bot token can be checked via read-only auth.test after readiness.

Inspect /workspace/friday-os and /workspace/work/friday-os-checkpoint.tar.gz before restoration. Preserve existing work. If the correct checkout exists, read its HANDOFF.md and status before doing anything. Otherwise inspect the source archive member paths and extract into an empty staging directory. Do not use the older /workspace/Friday-Ai build. Read HANDOFF.md plus all six project documents after extraction. Source archives omit Git history, dependencies and secrets.

The database dump contains the synthetic local ledger and audit records through the successful Milestone 4 retry. Milestone 5 used only disposable test databases and did not change that ledger. Restore this dump only into a fresh isolated Friday database if the original volume is unavailable. Never overwrite an existing database. Generate new local database/API credentials privately if restoring; these are intentionally absent from the bundle.

Current tests: 65 offline tests passed, plus real disposable-PostgreSQL deduplication/recovery checks. No live Slack connection, event Request URL or tunnel exists yet. All Slack identity allowlists still require configuration. The source's corrected creation manifest omits event_subscriptions; add app_mention only once the HTTPS request URL is ready. Some older append-only documentation sections describe superseded states; the Slack installation confirmation and latest milestone sections govern.

Keep Slack disabled until credentials, identities and signed ingress are verified. Continue the single-agent advisory scope: no customer data, CRM access, payments, customer communications, schedules or autonomous workflows. No Railway deployment; that is Milestone 6 and requires separate confirmation. Explain implementation steps and tests, save as you go, and wait at milestone boundaries.
