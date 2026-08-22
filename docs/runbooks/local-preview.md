# Local preview

This workflow runs the UI against the deterministic development API. It does
not call LinkedIn, Apollo, or other providers.

1. Start the infrastructure and deterministic API from the repository root:

   ```bash
   docker compose up -d postgres redis minio
   cd backend
   ./.venv/bin/uvicorn tests.e2e_task13_api:app --app-dir . --host 127.0.0.1 --port 8001
   ```

2. In another terminal, start the web app:

   ```bash
   cd web
   env \
     API_BASE_URL=http://127.0.0.1:8001 \
     AUTH_URL=http://127.0.0.1:3000 \
     AUTH_SECRET="$(openssl rand -base64 48 | tr -d '\n' | tr '+/' '-_')" \
     OIDC_ISSUER=http://127.0.0.1:8001/oidc \
     OIDC_AUDIENCE=http://127.0.0.1:8001/api \
     OIDC_CLIENT_ID=e2e-client \
     OIDC_CLIENT_SECRET=e2e-client-credential \
     'TENANT_OPTIONS=[{"id":"00000000-0000-4000-8000-000000000001","name":"E2E Agency"}]' \
     ENABLE_DEV_PREVIEW=true \
     ENABLE_DEV_AUTH_OVERRIDE=true \
     npm run dev -- --hostname 127.0.0.1
   ```

3. Open <http://127.0.0.1:3000/dev-preview?view=task13>.

The override is disabled unless explicitly enabled, accepts only literal
loopback hosts, and is rejected by production configuration checks. Remove
`ENABLE_DEV_AUTH_OVERRIDE=true` to exercise normal company authentication.
