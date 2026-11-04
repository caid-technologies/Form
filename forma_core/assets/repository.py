"""Immutable metadata in the already selected application database."""
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from forma_core.persistence.models import DBComponentAssetVersion


class AssetRepository:
    def __init__(self, provider):
        self.provider = provider
        provider.initialize()

    def find(self, scope: str, identity_key: str, *, family=False):
        if self.provider.backend == "sqlite":
            with self.provider.session_factory() as session:
                rows = session.execute(select(DBComponentAssetVersion).where(
                    DBComponentAssetVersion.scope_key == scope,
                    (DBComponentAssetVersion.family_key if family else DBComponentAssetVersion.identity_key) == identity_key,
                ).order_by(DBComponentAssetVersion.version).limit(101)).scalars()
                return [row.payload_json for row in rows]
        result = (self.provider.client.table("component_asset_versions").select("payload_json")
                  .eq("scope_key", scope).eq("family_key" if family else "identity_key", identity_key).order("version").limit(101).execute())
        return [row["payload_json"] for row in result.data]

    def get(self, scope: str, asset_id: str, version: str):
        if self.provider.backend == "sqlite":
            with self.provider.session_factory() as session:
                row = session.execute(select(DBComponentAssetVersion).where(
                    DBComponentAssetVersion.scope_key == scope,
                    DBComponentAssetVersion.asset_id == asset_id,
                    DBComponentAssetVersion.version == version,
                )).scalar_one_or_none()
                return row.payload_json if row else None
        result = (self.provider.client.table("component_asset_versions").select("payload_json")
                  .eq("scope_key", scope).eq("asset_id", asset_id).eq("version", version).limit(1).execute())
        return result.data[0]["payload_json"] if result.data else None

    def insert(self, scope: str, identity_key: str, asset):
        row = dict(scope_key=scope, identity_key=identity_key, family_key=asset.registration.identity.family_key, asset_id=asset.asset_id,
                   version=asset.version, payload_json=asset.model_dump(mode="json"), created_at=asset.created_at)
        if self.provider.backend == "sqlite":
            with self.provider.session_factory() as session:
                session.execute(insert(DBComponentAssetVersion).values(**row).on_conflict_do_nothing())
                session.commit()
        else:
            self.provider.client.table("component_asset_versions").upsert(
                row, on_conflict="asset_id,version", ignore_duplicates=True).execute()
        return self.get(scope, asset.asset_id, asset.version)
