# syntax=docker/dockerfile:1

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SQLITE_DATABASE_URL=sqlite:////data/forma.db \
    A2A_SOCKET_HOST=0.0.0.0

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app

COPY apps/api/requirements.txt ./apps/api/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r apps/api/requirements.txt

COPY apps/__init__.py ./apps/__init__.py
COPY apps/api ./apps/api
COPY forma_core ./forma_core
COPY .agents/skills/forma-hardware/scripts/cad.py ./.agents/skills/forma-hardware/scripts/cad.py
# Install the native CAD kernel at build time, never in a live authoring request.
RUN pip install 'opencad[occt] @ git+https://github.com/caid-technologies/OpenCAD.git@ce31b40a3f6094a6993d9b7c0a734fb4df2eb161#subdirectory=packages/opencad' \
    && python .agents/skills/forma-hardware/scripts/cad.py check
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000 8766

CMD ["sh", "./apps/api/entrypoint.sh"]
