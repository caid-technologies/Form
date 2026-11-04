-- Server-only metadata; existing private cli-project-artifacts bucket holds bytes.
create table public.component_asset_versions (
    asset_id text not null,
    version text not null,
    scope_key text not null,
    identity_key text not null,
    family_key text not null,
    payload_json jsonb not null,
    created_at text not null,
    primary key (asset_id, version)
);
create index component_asset_versions_scope_identity
    on public.component_asset_versions (scope_key, identity_key);
create index component_asset_versions_scope_family
    on public.component_asset_versions (scope_key, family_key);
alter table public.component_asset_versions enable row level security;
revoke all on public.component_asset_versions from public, anon, authenticated;
grant select, insert on public.component_asset_versions to service_role;
-- No UPDATE privilege: refresh/revalidation creates another immutable version.
