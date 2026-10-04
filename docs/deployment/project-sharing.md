# Versioned project links

Project sharing uses the existing database. No additional environment variable or
signing secret is required. Each Share action creates a random 256-bit token;
only its SHA-256 hash is stored with the owner, project, and immutable revision.
Tokens are returned once at creation and are never returned by the management API.

## Deployment

For hosted Supabase deployments, apply
`supabase/migrations/20260924144814_project_share_tokens.sql` before deploying the
backend. The table is checked at startup, so a missing migration prevents startup.
SQLite creates the new table automatically at startup. Keep the existing database
persistent across restarts and shared by API replicas.

The table has RLS enabled and no access for `PUBLIC`, `anon`, or `authenticated`;
only the server's service role can read or modify it. The application checks owner
identity for link creation, listing, and revocation. Supabase client keys do not
authorize these operations directly.

Links created by the earlier signing-key draft must be recreated; there is no
fallback that accepts old signatures. Any previous share-signing secret can be
removed after updating all API replicas.

## Sharing and revocation

Open **Share** on the displayed saved version, then **Create and copy link**.
The dialog also displays the new URL if clipboard access is unavailable.
Keep the link: the server cannot recover it from its stored hash. Returning to the
version lists link IDs and creation times, with a **Revoke** action for each link.
Only the owner may list or revoke links. Revocation is idempotent and affects just
that link; other links for the same version continue to work.

The link uses `/project/<id>/shared?revision=<revision UUID>#share=<token>`.
The fragment keeps the token out of HTTP request URLs and referrers. The recipient
page sends it using `X-Project-Share`; reverse proxies must forward this header.
API CORS permits it. Snapshot and CAD responses use `Cache-Control: private, no-store`.

Anyone with the complete link can view that design version and its saved CAD,
without signing in. Sharing does not change project visibility, publish chat,
permit editing, list history, or authorize other versions. The viewer omits
private generation metadata, uploaded chat reference imagery, and revision
instructions. Later edits do not change the shared snapshot. Missing, invalid,
or revoked links fail explicitly instead of loading the latest design. Legacy
projects without immutable revisions must save a version before sharing.

Deleting a project makes its links unavailable; hard deletion of a project or
revision removes its share records through foreign-key cascades. Revocation blocks
future snapshot and CAD requests. It cannot erase content someone already viewed
or downloaded, including an image URL already issued before revocation.

## API

- `POST /projects/{project_id}/shared/{revision_id}`: owner-only creation; returns
  link ID, revision ID, creation time, revocation state, and the raw token once.
- `GET /projects/{project_id}/shared/{revision_id}/links`: owner-only metadata list;
  accepts `limit` (1–100, default 50) and `offset`, and returns `items` and
  `next_offset`. Neither tokens nor hashes are returned.
- `DELETE /projects/{project_id}/shared/{revision_id}/links/{share_id}`: owner-only,
  idempotent revocation, returning 204.
- `GET /projects/{project_id}/shared/{revision_id}` and its `/cad/{sha256}` route:
  require the unrevoked token in `X-Project-Share` and authorize only that revision.
