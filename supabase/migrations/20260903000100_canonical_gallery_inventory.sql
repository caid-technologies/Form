-- Bounded gallery read surface. Canonical identities win over legacy projections.

create index if not exists idx_projects_gallery_status_visibility_updated
  on public.projects (status, visibility, updated_at desc);

create index if not exists idx_project_revisions_project_revision
  on public.project_revisions (project_id, revision desc);

create or replace view public.project_gallery_inventory as
with hosted_latest as (
  select distinct on (project_id)
    id as revision_id,
    project_id,
    owner_user_id,
    revision,
    payload_json as revision_payload_json,
    created_at as revision_created_at
  from public.project_revisions
  order by project_id, revision desc
), cli_latest as (
  select distinct on (project_id)
    revision_id,
    project_id,
    owner_user_id,
    revision,
    manifest_json as revision_payload_json,
    created_at as revision_created_at
  from public.cli_project_revisions
  order by project_id, revision desc
)
select
  p.project_id,
  p.owner_user_id,
  p.creation_channel,
  p.title,
  p.prompt,
  p.chat_id,
  p.workspace_id,
  p.visibility,
  p.status,
  p.created_at,
  coalesce(r.revision_created_at, p.updated_at) as updated_at,
  'canonical'::text as source,
  r.revision_id,
  r.revision,
  r.revision_payload_json,
  r.revision_created_at,
   null::jsonb as legacy_hardware_ir,
  null::integer as legacy_id
from public.projects p
join hosted_latest r on r.project_id = p.project_id
                     and r.owner_user_id = p.owner_user_id
where p.creation_channel <> 'cli'
union all
select
  p.project_id,
  p.owner_user_id,
  p.creation_channel,
  p.title,
  p.prompt,
  p.chat_id,
  p.workspace_id,
  p.visibility,
  p.status,
  p.created_at,
  coalesce(r.revision_created_at, p.updated_at) as updated_at,
  'canonical'::text as source,
  r.revision_id,
  r.revision,
  r.revision_payload_json,
  r.revision_created_at,
   null::jsonb as legacy_hardware_ir,
  null::integer as legacy_id
from public.projects p
join public.cli_projects cp on cp.project_id = p.project_id
                            and cp.owner_user_id = p.owner_user_id
join cli_latest r on r.project_id = cp.project_id
                   and r.owner_user_id = cp.owner_user_id
where p.creation_channel = 'cli'
  and (cp.current_revision_id is null or r.revision_id = cp.current_revision_id)
union all
select
  g.project_id,
  g.owner_user_id,
  g.creation_channel,
  g.title,
  g.prompt,
  g.chat_id,
  null::text as workspace_id,
  g.visibility,
  g.status,
  g.created_at,
  g.created_at as updated_at,
  'legacy'::text as source,
  null::text as revision_id,
  null::integer as revision,
  null::jsonb as revision_payload_json,
  null::text as revision_created_at,
  null::jsonb as legacy_hardware_ir,
  g.id as legacy_id
from public.generated_projects g
where g.status = 'active'
  and not exists (
    select 1
    from public.project_revisions r
    where r.project_id = g.project_id
      and r.owner_user_id = g.owner_user_id
  )
  and not exists (
    select 1
    from public.cli_project_revisions r
    where r.project_id = g.project_id
      and r.owner_user_id = g.owner_user_id
  );

revoke all on public.project_gallery_inventory from anon, authenticated;
grant select on public.project_gallery_inventory to service_role;

comment on view public.project_gallery_inventory is
  'Canonical gallery inventory with current revisions and active legacy fallback rows only when no identity exists.';
