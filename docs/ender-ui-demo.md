# Ender 3 platform demo

Scope: the real project Exports UI downloads stored STEP, starts a real Ender 3
slice on the mini-PC, polls its durable job, and downloads project-owned G-code.
No Bambu configuration, direct printing, new CAD implementation, or startup-task
work is required. local-server-config #50 must already be running on the mini-PC.

## Server configuration

Set these PRIVATE variables on the deployment that runs the Forma Python API:

- `FORMA_ENDER_WORKER_URL`: worker origin reachable from that API (HTTPS remotely).
- `FORMA_ENDER_WORKER_TOKEN`: the running worker's `FORMA_SLICER_TOKEN`.

Never use `NEXT_PUBLIC_` for either variable or send the token to the browser.
Do not use `127.0.0.1:8790` on a cloud deployment: that refers to the cloud host,
not the mini-PC. Loopback is supported only when the API runs on the mini-PC.
Keep the slicer bound to loopback. Use a controller-approved authenticated HTTPS
route/tunnel; no public listener or tunnel is created by this PR.

For a temporary recording session, the operator can explicitly choose a Cloudflare
Quick Tunnel. This publishes the existing worker via a random HTTPS hostname;
all worker routes except liveness still require its bearer token. This is demo
exposure, not the permanent host topology. Do not change existing managed tunnels,
firewall rules, or service accounts. Stop the temporary tunnel after recording.
See https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/

With the worker already running, in a separate regular PowerShell window:

```powershell
& 'C:\Program Files\cloudflared\cloudflared.exe' tunnel --url http://127.0.0.1:8790
```

Use the emitted HTTPS origin as `FORMA_ENDER_WORKER_URL`. In the original session
that holds the worker token, copy it without printing it:

```powershell
if (-not $env:FORMA_SLICER_TOKEN) { throw 'Use the existing worker session.' }
Set-Clipboard -Value $env:FORMA_SLICER_TOKEN
```

Paste only into the API deployment's secret/environment configuration, then clear
the clipboard with `Set-Clipboard -Value ''`. Do not paste it into chat, source,
logs, command arguments, or a frontend/public environment variable. Redeploy both
API and web from this PR. For a Vercel monorepo, configure the API project, not
only the web project. The hostname changes when a Quick Tunnel is recreated.

## Record the demo

Open an owned project with a saved STEP in the platform. Go to **Exports**.
Only **Creality Ender-3 / 0.4 mm / PLA / 0.20 mm** is offered when bridge mode is
enabled. Click **Generate G-code**; queued/running states come from the real job.
On completion, show the bounded preview and click **Download G-code**. The API
checks the worker download checksum and existing G-code validation, then persists
G-code and a provenance report in the project's artifact storage. Existing project
ownership/authentication and checksum checks protect subsequent downloads.

The UI does not talk directly to the mini-PC. The worker secret and local paths
are not included in manifest/job responses. Missing or offline workers disable
Ender availability rather than falling back to cloud Orca. Polling stops on
navigation; this does not cancel an already uploaded worker job. New Generate
clicks create new jobs so a failed prior idempotency key is not reused.

A saved model revision change rejects an old job. Browser cached results are
keyed to project, printer, and source STEP checksum. The worker does not supply a
profile-file fingerprint, so this bridge returns null rather than inventing one.
A successful export is not printer safety certification. Inspect in PrusaSlicer
before any physical print. No print-start control exists here.

Tests: `python -m pytest tests/projects/test_slicing_worker_bridge.py tests/projects/test_ender_exports_api.py -q`
and `node --test apps/web/test/export-job.test.ts`. Tests use a fake HTTP worker;
actual hosted UI → deployed tunnel → mini-PC → artifact storage acceptance still
requires the operator's browser and deployment configuration.
