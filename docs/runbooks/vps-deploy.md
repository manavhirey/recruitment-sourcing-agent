# VPS deployment runbook (kunigami.cloud)

Single-host deployment of dev + production environments behind an existing
Traefik reverse proxy, with GitHub Actions CI/CD.

## Layout

```
Traefik (host ports 80/443, network: traefik-proxy)
├── auth.kunigami.cloud  → Keycloak (OIDC)      /opt/recruitment/keycloak
├── kunigami.cloud       → prod web             /opt/recruitment/prod
├── api.kunigami.cloud   → prod api
├── dev.kunigami.cloud   → dev web              /opt/recruitment/dev
└── dev-api.kunigami.cloud → dev api
```

Each environment directory contains:
- `repo/` — bare-ish git checkout of this repository (deploy checks out the exact ref)
- `.env` — secrets + per-env config (from `ops/deploy/env.template`, never committed)

## Environments & CI/CD

- **dev** — `.github/workflows/deploy-dev.yml` triggers on PR open/push against
  `main`, builds `recruitment-sourcing-api` / `-web` images tagged `pr-<n>`,
  pushes to GHCR, SSHes to the VPS and runs `ops/deploy/deploy.sh dev`.
- **production** — `.github/workflows/deploy-prod.yml` triggers via
  `workflow_run` after "Release gates" passes on `main`, images tagged
  `main-<sha>`, deploys with `deploy.sh prod` (GitHub environment
  `production` can require manual approval).

## Required GitHub configuration

- Repository variable: `VPS_HOST` = `2.24.108.176` (optionally `VPS_USER` = `deploy`)
- Repository secret: `VPS_SSH_KEY` — private key for the `deploy` user
- Environments: `dev`, `production` (add required reviewers to `production`)

## VPS provisioning (one-time)

```bash
sudo usermod -aG docker ramon                      # docker access for ramon
sudo useradd -m -G docker deploy                   # CI deploy user
# install deploy user's public key (from GitHub secret) in ~deploy/.ssh/authorized_keys

sudo mkdir -p /opt/recruitment/{dev,prod,keycloak}
sudo chown -R deploy:docker /opt/recruitment       # deploy user runs compose here
docker network create --subnet 172.28.0.0/16 traefik-proxy

# per environment:
sudo -u deploy git clone https://github.com/manavhirey/recruitment-sourcing-agent.git /opt/recruitment/<env>/repo
# write /opt/recruitment/<env>/.env from ops/deploy/env.template (chmod 600)

# GHCR login for private images:
sudo -u deploy sh -c 'echo <PAT> | docker login ghcr.io -u manavhirey --password-stdin'
```

## Deploy manually

```bash
sudo -u deploy /opt/recruitment/dev/repo/ops/deploy/deploy.sh dev refs/pull/N/head pr-N
sudo -u deploy /opt/recruitment/prod/repo/ops/deploy/deploy.sh prod <sha> main-<sha>
```

## Rollback

```bash
# redeploy a previous image tag / ref
sudo -u deploy .../deploy.sh <env> <git-ref> <previous-tag>
```
