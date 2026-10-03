-- Private object storage for files referenced by CLI project revisions.

insert into storage.buckets (id, name, public, file_size_limit)
values ('cli-project-artifacts', 'cli-project-artifacts', false, 52428800)
on conflict (id) do update
set public = excluded.public,
    file_size_limit = excluded.file_size_limit;
