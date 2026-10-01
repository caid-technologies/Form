# Project deletion and sanitized contributions

This document describes the implementation introduced for issue #160. Legal, privacy, security, and anonymization reviewers must approve the policy text and standards before sanitized contributions are enabled for production dataset use.

## Lifecycle

- `DELETE /projects/{project_id}` changes an owned active project to `deletion_pending`, hides it from normal reads immediately, cancels project-linked jobs, and sets `purge_after` (30 days by default).
- `POST /projects/{project_id}/restore` restores a pending project only before purge starts.
- A background worker claims due projects, removes remote images, current and legacy videos, durable jobs, project-linked chat data, consent identifiers, and the project row. Failed stages set `deletion_failed` and are retried idempotently.
- Reads and writes use active-project repository guards. Bearer authentication prevents cookie-based CSRF, and destructive and consent-changing routes require a token issued within the configured recent-auth window.
- The admin-only `/admin/project-deletions` view contains lifecycle metadata and counts, never project content.

This repository currently has no project share-link, ACL, upload, cache, search-index, or vector/embedding store. If one is added, its project-scoped delete adapter must be included before the database purge is allowed to complete.

## Contribution boundary

Consent is explicit, versioned, purpose-specific, off by default, and stored separately. Deletion succeeds without consent. When active consent exists, deletion creates only an aggregate summary of component categories and structural counts. Prompts, titles, identifiers, URLs, credentials, uploads, request metadata, and free text are never copied into the contribution store.

Before purge, withdrawal deletes the pending snapshot. At purge, an eligible sanitized snapshot receives unrelated random source and consent identifiers and is severed from the consent/account record; the identifiable consent row is then deleted. Dataset export must accept only snapshots with `contribution_status=anonymized` and a completed anonymization review.

## Configuration and operations

- `PROJECT_DELETION_RETENTION_DAYS` defaults to `30`.
- `PROJECT_PURGE_INTERVAL_SECONDS` defaults to `60` and has a minimum of 10 seconds.
- `PROJECT_PURGE_STALE_AFTER_SECONDS` defaults to `3600` before a crashed `purging` claim may be reclaimed.
- `PROJECT_PURGE_ALERT_AFTER_SECONDS` defaults to `3600`; overdue due-projects emit an error log suitable for alerting.
- `DESTRUCTIVE_AUTH_MAX_AGE_SECONDS` defaults to `600`.

Alert when `deletion_failed` events occur or a due `deletion_pending`/`purging` project remains beyond one worker interval. Retry by allowing the worker to run; cleanup stages are prefix-scoped and idempotent. Investigators may use the admin audit route, application logs, and the content-free `error_type` without exposing project content.

Purge claims use the canonical `projects` lifecycle and lease timestamp when that row exists. A legacy `generated_projects` row can still identify overdue work, but its stale status cannot override a restored canonical project or an active purge claim. A stale canonical claim is reclaimed using its own timestamp, and the successful claim synchronizes the legacy projection before storage cleanup. Projects with only a legacy row retain the legacy cleanup path.

## Backups and recovery

Production owners must configure encrypted backup expiry no longer than the approved retention schedule and document provider-specific expiration. Deleted content in an immutable backup is unavailable to normal product systems and expires with that backup. Any disaster-recovery restore must replay deletion/audit records and complete due purges before user traffic or downstream dataset export resumes. Legal/security preservation exceptions must be narrowly approved, access-restricted, time-bound, and excluded from research use.

## Release gates

- Legal counsel approval of the privacy changes, consent language, and limited content license.
- Approval of the retention, sanitization, anonymization, re-identification, and malicious-dataset-content standards.
- Confirmation that every production subprocessor and storage system has equivalent deletion behavior.
- Security review of rate limiting, recent authentication, storage encryption, administrative access, and purge alerts.
- Dataset export and AI-training use remain out of scope for this implementation.
