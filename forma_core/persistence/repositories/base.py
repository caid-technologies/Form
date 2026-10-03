from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class ApplicationRepository(Protocol):
    """Persistence operations used by the application domain."""

    def count_component_templates(self) -> int: ...

    def list_component_templates(self) -> List[Any]: ...

    def get_component_template_by_part_number(self, part_number: str) -> Optional[Any]: ...

    def insert_component_template(self, record: Dict[str, Any]) -> None: ...

    def upsert_project_identity(self, record: Dict[str, Any]) -> Any: ...

    def get_project_identity(self, project_id: str) -> Optional[Any]: ...

    def list_project_identities(self, owner_user_id: str) -> List[Any]: ...

    def list_project_gallery_inventory_page(
        self,
        owner_user_id: Optional[str],
        *,
        visibility: Optional[str],
        limit: int,
        offset: int,
        search: Optional[str] = None,
    ) -> tuple[List[Any], int]: ...

    def list_project_gallery_inventory(
        self,
        owner_user_id: Optional[str],
        *,
        visibility: Optional[str],
        search: Optional[str] = None,
    ) -> List[Any]: ...

    def save_generated_project(
        self,
        record: Dict[str, Any],
        chat_record: Optional[Dict[str, Any]],
    ) -> None: ...

    def list_generated_projects(self, owner_user_id: Optional[str]) -> List[Any]: ...

    def list_generated_projects_page(
        self,
        owner_user_id: Optional[str],
        *,
        visibility: Optional[str],
        limit: int,
        offset: int,
        search: Optional[str] = None,
    ) -> tuple[List[Any], int]: ...

    def get_generated_project(self, project_id: str, include_deleted: bool = False) -> Optional[Any]: ...

    def insert_design_brief_version(self, record: Dict[str, Any]) -> Any: ...

    def list_design_brief_versions(self, project_id: str, owner_user_id: str) -> List[Any]: ...

    def get_design_brief_version(
        self,
        project_id: str,
        owner_user_id: str,
        brief_version: int,
    ) -> Optional[Any]: ...

    def get_latest_design_brief(self, project_id: str, owner_user_id: Optional[str]) -> Optional[Any]: ...

    def get_project_workflow(self, project_id: str, owner_user_id: Optional[str]) -> Optional[Any]: ...

    def list_project_workflow_transitions(self, project_id: str, owner_user_id: str) -> List[Any]: ...

    def get_project_workflow_transition_by_idempotency(
        self,
        project_id: str,
        owner_user_id: str,
        idempotency_key: str,
    ) -> Optional[Any]: ...

    def apply_project_workflow_transition(
        self,
        state_record: Dict[str, Any],
        transition_record: Dict[str, Any],
        expected_state: Optional[str],
        expected_revision: Optional[int],
    ) -> Optional[tuple[Any, Any]]: ...

    def get_project_build_by_idempotency(
        self,
        project_id: str,
        owner_user_id: str,
        idempotency_key: str,
    ) -> Optional[Any]: ...

    def get_latest_project_build(self, project_id: str, owner_user_id: str) -> Optional[Any]: ...

    def apply_project_build_initiation(
        self,
        state_record: Dict[str, Any],
        transition_record: Dict[str, Any],
        build_record: Dict[str, Any],
        expected_state: str,
        expected_revision: int,
    ) -> Optional[tuple[Any, Any, Any]]: ...

    def insert_worker_execution_plan(self, record: Dict[str, Any]) -> Any: ...

    def get_worker_execution_plan(self, plan_id: str, owner_user_id: str) -> Optional[Any]: ...

    def list_worker_execution_plans(self, limit: int = 200) -> List[Any]: ...

    def update_worker_execution_plan(
        self,
        plan_id: str,
        owner_user_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Any]: ...

    def get_latest_project_revision(self, project_id: str, owner_user_id: str) -> Optional[Any]: ...

    def list_latest_project_revisions(self, owner_user_id: str) -> List[Any]: ...

    def list_project_revisions(self, project_id: str, owner_user_id: str, *, limit: int, before: int | None = None) -> List[Any]: ...

    def get_project_revision_by_id(self, project_id: str, owner_user_id: str, revision_id: str) -> Optional[Any]: ...

    def get_project_revision(
        self,
        project_id: str,
        owner_user_id: str,
        revision: int,
    ) -> Optional[Any]: ...

    def insert_project_revision(
        self,
        record: Dict[str, Any],
        expected_parent_revision: int,
    ) -> Optional[Any]: ...

    def get_project_revision_by_source_job(
        self,
        project_id: str,
        owner_user_id: str,
        source_job_id: str,
    ) -> Optional[Any]: ...

    def insert_initial_project_revision(self, record: Dict[str, Any]) -> Optional[Any]: ...

    def get_cli_project(self, project_id: str, owner_user_id: str) -> Optional[Any]: ...

    def update_cli_project_visibility(
        self,
        project_id: str,
        owner_user_id: str,
        visibility: str,
    ) -> Optional[Any]: ...

    def list_cli_projects(self, owner_user_id: str) -> List[Any]: ...

    def get_cli_project_revision(
        self,
        project_id: str,
        owner_user_id: str,
        revision_id: Optional[str] = None,
    ) -> Optional[Any]: ...

    def insert_cli_project_revision(
        self,
        project_record: Dict[str, Any],
        revision_record: Dict[str, Any],
        expected_revision_id: Optional[str],
    ) -> Optional[Any]: ...

    def get_cli_project_delivery(
        self,
        project_id: str,
        owner_user_id: str,
        idempotency_key: str,
    ) -> Optional[Any]: ...

    def get_cli_project_delivery_by_id(self, delivery_id: str) -> Optional[Any]: ...

    def list_cli_project_deliveries(self, owner_user_id: str) -> List[Any]: ...

    def insert_cli_project_delivery(self, record: Dict[str, Any]) -> Any: ...

    def update_cli_project_delivery(
        self,
        delivery_id: str,
        owner_user_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Any]: ...

    def record_project_publish_audit(self, record: Dict[str, Any]) -> None: ...

    def list_project_publish_audits(self, project_id: str, owner_user_id: str) -> List[Any]: ...

    def get_cli_device_authorization(self, device_code_hash: Optional[str] = None, user_code_hash: Optional[str] = None) -> Optional[Any]: ...

    def insert_cli_device_authorization(self, record: Dict[str, Any]) -> Any: ...

    def update_cli_device_authorization(
        self,
        device_code_hash: str,
        updates: Dict[str, Any],
        expected_status: Optional[str] = None,
        expected_consumed: Optional[bool] = None,
    ) -> Optional[Any]: ...

    def get_cli_token_session(self, token_hash: str) -> Optional[Any]: ...

    def insert_cli_token_session(self, record: Dict[str, Any]) -> Any: ...

    def revoke_cli_token_sessions(
        self,
        *,
        token_hash: Optional[str] = None,
        refresh_token_hash: Optional[str] = None,
        revoked_at: float,
    ) -> int: ...

    def get_validation_report(self, report_id: str, owner_user_id: str) -> Optional[Any]: ...

    def get_validation_report_by_source_job(
        self,
        project_id: str,
        owner_user_id: str,
        source_job_id: str,
    ) -> Optional[Any]: ...

    def insert_project_validation_report(self, record: Dict[str, Any]) -> Optional[Any]: ...

    def list_due_project_purges(self, before: str, limit: int) -> List[Any]: ...

    def update_project_deletion_state(
        self,
        project_id: str,
        owner_user_id: Optional[str],
        allowed_statuses: List[str],
        updates: Dict[str, Any],
        expected_purge_started_at: Optional[str] = None,
    ) -> Optional[Any]: ...

    def hard_purge_project(self, project_id: str, owner_user_id: Optional[str]) -> bool: ...

    def update_generated_project_hardware_ir(
        self,
        project_id: str,
        hardware_ir: Dict[str, Any],
        chat_id: Optional[str],
        owner_user_id: Optional[str],
    ) -> bool: ...

    def claim_unowned_generated_project(
        self,
        project_id: str,
        hardware_ir: Dict[str, Any],
        chat_id: Optional[str],
        owner_user_id: str,
    ) -> bool: ...

    def update_generated_project_metadata(
        self,
        project_id: str,
        owner_user_id: str,
        updates: Dict[str, Any],
    ) -> bool: ...

    def delete_generated_project(self, project_id: str, owner_user_id: str) -> bool: ...

    def get_project_contribution_consent(self, project_id: str, user_id: str) -> Optional[Any]: ...

    def upsert_project_contribution_consent(self, record: Dict[str, Any]) -> Any: ...

    def withdraw_project_contribution_consent(self, project_id: str, user_id: str, withdrawn_at: str) -> Optional[Any]: ...

    def anonymize_project_contribution_consent(
        self,
        project_id: str,
        user_id: str,
        anonymized_project_id: str,
        anonymized_user_id: str,
        anonymized_at: str,
    ) -> bool: ...

    def upsert_project_contribution_snapshot(self, record: Dict[str, Any]) -> Any: ...

    def anonymize_project_contribution_snapshot(
        self,
        consent_record_id: str,
        anonymized_source_id: str,
        anonymized_consent_id: str,
        anonymized_at: str,
    ) -> bool: ...

    def purge_project_contribution_snapshots(self, consent_record_id: str, purged_at: str) -> int: ...

    def add_project_deletion_audit(self, record: Dict[str, Any]) -> Any: ...

    def get_latest_project_deletion_audit(self, project_id: str) -> Optional[Any]: ...

    def list_project_deletion_audits(self, limit: int) -> List[Any]: ...

    def upsert_project_chat(self, record: Dict[str, Any]) -> Any: ...

    def list_project_chats(self, owner_user_id: str) -> List[Any]: ...

    def get_project_chat(self, chat_id: str, owner_user_id: str) -> Optional[Any]: ...

    def delete_project_chat(self, chat_id: str, owner_user_id: str) -> bool: ...

    def save_alpha_signup(self, record: Dict[str, Any]) -> Any: ...

    def insert_project_save(self, record: Dict[str, Any]) -> bool: ...

    def delete_project_save(self, project_id: str, owner_user_id: str) -> bool: ...

    def count_project_saves(self, project_ids: List[str]) -> Dict[str, int]: ...

    def list_saved_project_ids(self, owner_user_id: str, project_ids: List[str]) -> List[str]: ...

    def insert_project_remix(self, record: Dict[str, Any]) -> Any: ...

    def count_project_remixes(self, project_ids: List[str]) -> Dict[str, int]: ...

    def get_user_settings(self, owner_user_id: str) -> Optional[Any]: ...

    def upsert_user_settings(self, record: Dict[str, Any]) -> Any: ...

    def list_model_training_opt_out_user_ids(self) -> List[str]: ...
