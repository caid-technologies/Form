#!/bin/sh
set -eu

if [ -z "${FORMA_USER_SECRETS_KEY:-}" ]; then
    key_file="${FORMA_LOCAL_SECRETS_FILE:-/data/.forma_user_secrets_key}"
    if [ -s "$key_file" ]; then
        FORMA_USER_SECRETS_KEY="$(cat "$key_file")"
    else
        umask 077
        mkdir -p "$(dirname "$key_file")"
        FORMA_USER_SECRETS_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
        printf '%s\n' "$FORMA_USER_SECRETS_KEY" > "$key_file"
    fi
    export FORMA_USER_SECRETS_KEY
fi

exec uvicorn apps.api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
