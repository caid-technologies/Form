#!/usr/bin/env python3
"""Validate Forma production environment variables without printing secrets."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[2]
TRUTHY = {"1", "true", "yes", "on"}
REDACTED_VALUE = "[SENSITIVE]"
AUTH_ALLOWLIST_KEYS = (
    "FORMA_ADMIN_USER_IDS",
    "CLERK_ADMIN_USER_IDS",
    "FORMA_ADMIN_EMAILS",
    "CLERK_ADMIN_EMAILS",
)
SERVICE_TOKEN_KEYS = ("FORMA_MCP_API_KEY", "FORMA_A2A_API_KEY")
PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "LLM_API_KEY"),
    "openai": ("OPENAI_API_KEY", "LLM_API_KEY"),
    "baseten": ("BASETEN_API_KEY", "LLM_API_KEY"),
    "gmi": ("GMI_API_KEY", "GMI_CLOUD_API_KEY", "GMICLOUD_API_KEY", "LLM_API_KEY"),
    "huggingface": ("HUGGINGFACE_API_KEY", "HUGGINGFACE_HUB_TOKEN", "HF_TOKEN", "HF_API_TOKEN", "LLM_API_KEY"),
    "cloudflare": ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_AI_API_KEY", "CLOUDFLARE_API_KEY", "LLM_API_KEY"),
    "nvidia": ("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "NIM_API_KEY", "LLM_API_KEY"),
    "runpod": ("RUNPOD_API_KEY", "LLM_API_KEY"),
    "runpod-serverless": ("RUNPOD_API_KEY",),
    "openai-compatible": ("LLM_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY"),
    "simulation": (),
    "vertex": ("GOOGLE_CLOUD_PROJECT", "VERTEX_AI_PROJECT", "GCP_PROJECT_NUMBER"),
}


class CheckReport:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []
        self.warnings: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.results.append((label, ok, detail))

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def print(self) -> None:
        for label, ok, detail in self.results:
            status = "PASS" if ok else "FAIL"
            suffix = f" ({detail})" if detail else ""
            print(f"{status}\t{label}{suffix}")
        for warning in self.warnings:
            print(f"WARN\t{warning}")
        passed = sum(1 for _, ok, _ in self.results if ok)
        print(f"SUMMARY\t{passed}/{len(self.results)} checks passed")
        failed = [label for label, ok, _ in self.results if not ok]
        if failed:
            print("FAILED\t" + ", ".join(failed))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.results)


def normalize_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    value = value.replace("\\n", "\n").strip()
    return value


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        env[key] = normalize_value(value)
    return env


def pull_vercel_env(environment: str, project: str | None = None) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    temp_dir = tempfile.TemporaryDirectory(prefix="forma-production-env-")
    env_path = Path(temp_dir.name) / ".env"
    vercel_command = shutil.which("vercel.cmd") or shutil.which("vercel") or "vercel"
    command = [vercel_command, "env", "pull", str(env_path), "--environment", environment, "--yes"]
    if project:
        command.extend(["--project", project])
    subprocess.run(command, cwd=ROOT_DIR, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return env_path, temp_dir


def first_present(env: dict[str, str], names: Iterable[str]) -> str | None:
    for name in names:
        if env.get(name, "").strip():
            return name
    return None


def is_true(env: dict[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in TRUTHY


def is_redacted(value: str) -> bool:
    return value.strip() == REDACTED_VALUE


def key_mode(value: str) -> str | None:
    if value.startswith(("pk_live_", "sk_live_")):
        return "live"
    if value.startswith(("pk_test_", "sk_test_")):
        return "test"
    return None


def validate(env: dict[str, str], *, require_live_clerk: bool) -> CheckReport:
    report = CheckReport()
    value = lambda name: env.get(name, "").strip()

    report.check("Supabase URL present", bool(value("SUPABASE_URL")))
    report.check("Supabase service/secret key present", bool(first_present(env, ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"))))
    report.check("Database backend is Supabase", value("DATABASE_BACKEND").lower() == "supabase", f"DATABASE_BACKEND={value('DATABASE_BACKEND') or '<unset>'}")
    redis_url = value("REDIS_URL")
    upstash_url = value("UPSTASH_REDIS_REST_URL")
    upstash_token = value("UPSTASH_REDIS_REST_TOKEN")
    redis_prefix = value("REDIS_CACHE_PREFIX")
    report.check(
        "Project cache configuration present",
        bool(redis_prefix and (redis_url or (upstash_url and upstash_token))),
    )
    local_backend_values = {"file", "local", "json", "sqlite", "sqlite3"}
    for label, name in (
        ("Workspace integrations are not file-backed", "FORMA_WORKSPACE_INTEGRATIONS_BACKEND"),
        ("User integrations are not file-backed", "FORMA_USER_INTEGRATIONS_BACKEND"),
        ("Image storage is not local", "FORMA_IMAGE_STORAGE_BACKEND"),
        ("CLI artifact storage is not local", "FORMA_CLI_ARTIFACT_STORAGE_BACKEND"),
        ("Shared integration storage is not file-backed", "FORMA_INTEGRATIONS_BACKEND"),
    ):
        configured = value(name).lower()
        report.check(label, configured not in local_backend_values, f"{name}={configured or '<unset>'}")
    deployment_mode = value("FORMA_DEPLOYMENT_MODE") or "local"
    report.check(
        "Deployment mode is hosted",
        deployment_mode.lower() == "hosted",
        f"FORMA_DEPLOYMENT_MODE={deployment_mode}",
    )
    report.check("Forma development mode disabled", not is_true(env, "FORMA_DEVELOPMENT_MODE") and not is_true(env, "FORMA_DEV_MODE"))
    report.check("Frontend dev mode disabled", not is_true(env, "NEXT_PUBLIC_FORMA_DEV_MODE"))
    report.check("Backend debug disabled", not is_true(env, "FORMA_DEBUG"))
    report.check("Frontend debug disabled", not is_true(env, "NEXT_PUBLIC_FORMA_DEBUG"))
    report.check(
        "Auth mode is Clerk",
        value("FORMA_AUTH_MODE").lower() == "clerk",
        f"FORMA_AUTH_MODE={value('FORMA_AUTH_MODE') or '<unset>'}",
    )
    report.check("Integration encryption key present", bool(value("FORMA_USER_SECRETS_KEY")))
    cors_value = value("FORMA_CORS_ORIGINS")
    cors_hidden = is_redacted(cors_value)
    if cors_hidden:
        report.warn("CORS origin allowlist is configured but hidden by Vercel; verify it with a live preflight")
        cors_value = ""
    cors_origins = [item.strip().rstrip("/") for item in cors_value.replace("\n", ",").split(",") if item.strip()]
    if not cors_hidden:
        report.check("Credentialed CORS origin allowlist present", bool(cors_origins))
        report.check("Credentialed CORS wildcard absent", "*" not in cors_origins)
        report.check(
            "Hosted CORS origins are HTTPS",
            bool(cors_origins) and all(
                urlparse(origin).scheme == "https" and bool(urlparse(origin).netloc)
                for origin in cors_origins
            ),
        )
    service_tokens = [name for name in SERVICE_TOKEN_KEYS if len(value(name)) >= 32 or is_redacted(value(name))]
    report.check("Server service credential present", bool(service_tokens), "FORMA_MCP_API_KEY or FORMA_A2A_API_KEY")
    authoring_value = value("FORMA_AUTHORING_MODE_ENABLED")
    if is_redacted(authoring_value):
        report.warn("Authoring mode is configured but hidden by Vercel; verify /api/runtime/config")
    else:
        report.check("Authoring mode explicitly enabled", is_true(env, "FORMA_AUTHORING_MODE_ENABLED"))
    report.check("Hosted chat gate is explicit", "FORMA_HOSTED_CHAT_ENABLED" in env)
    for name, minimum in (
        ("FORMA_MAX_REQUEST_BODY_BYTES", 1_048_576),
        ("FORMA_MAX_PROMPT_CHARS", 1_000),
        ("FORMA_MAX_IMAGE_ENCODED_CHARS", 1_024),
        ("FORMA_MAX_IMAGE_BYTES", 1_024),
        ("FORMA_MAX_IMAGE_DIMENSION", 64),
        ("FORMA_GENERATION_RATE_LIMIT", 1),
        ("FORMA_GENERATION_QUOTA", 1),
    ):
        if is_redacted(value(name)):
            report.warn(f"{name} is configured but hidden by Vercel; verify the live limit behavior")
            continue
        try:
            numeric_value = int(value(name))
        except ValueError:
            numeric_value = 0
        report.check(f"{name} bounded", numeric_value >= minimum)

    publishable_key_name = first_present(env, ("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "CLERK_PUBLISHABLE_KEY"))
    secret_key_name = first_present(env, ("CLERK_SECRET_KEY",))
    report.check("Clerk publishable key present", bool(publishable_key_name))
    report.check("Clerk secret key present", bool(secret_key_name))
    if not first_present(env, AUTH_ALLOWLIST_KEYS):
        report.warn("No admin allowlist configured; verify Clerk admin metadata is intentionally managed in production")

    if publishable_key_name:
        mode = key_mode(value(publishable_key_name))
        if mode == "test":
            message = f"{publishable_key_name} is a Clerk test key"
            if require_live_clerk:
                report.check("Clerk publishable key is live", False, message)
            else:
                report.warn(message)
    if secret_key_name:
        mode = key_mode(value(secret_key_name))
        if mode == "test":
            message = f"{secret_key_name} is a Clerk test key"
            if require_live_clerk:
                report.check("Clerk secret key is live", False, message)
            else:
                report.warn(message)

    provider = value("LLM_PROVIDER").lower()
    report.check("LLM provider selected", bool(provider))
    if provider == "simulation":
        report.check("Production not using simulation provider", False, "LLM_PROVIDER=simulation")
    elif provider in PROVIDER_KEYS:
        report.check(f"{provider} provider key present", bool(first_present(env, PROVIDER_KEYS[provider])))
    else:
        report.check("Recognized LLM provider", False, f"LLM_PROVIDER={provider or '<unset>'}")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="production", help="Vercel environment to pull when --env-file is omitted.")
    parser.add_argument("--project", default=None, help="Vercel project name or id to pull when --env-file is omitted.")
    parser.add_argument("--env-file", type=Path, default=None, help="Validate an existing dotenv file instead of pulling from Vercel.")
    parser.add_argument("--require-live-clerk", action="store_true", help="Fail if Clerk keys are test-mode keys.")
    args = parser.parse_args()

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.env_file:
            env_path = args.env_file.expanduser()
        else:
            env_path, temp_dir = pull_vercel_env(args.environment, args.project)
        env = parse_env_file(env_path)
        report = validate(env, require_live_clerk=args.require_live_clerk)
        report.print()
        return 0 if report.ok else 1
    except FileNotFoundError as exc:
        print(f"ERROR\tRequired command or file not found: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or str(exc)).strip()
        print(f"ERROR\tFailed to pull Vercel env: {message}", file=sys.stderr)
        return 2
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
