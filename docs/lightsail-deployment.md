# Theseus Lightsail deployment

Merging into `main` in `Theseus-Robotics/mlop` deploys the exact commit to
`ubuntu@18.197.48.243:/opt/mlop`. The workflow can also be run manually on `main`.
Deployments are serialized and superseded commits are skipped.

The server and web submodules remain in `flochkristof/mlop-server` and
`flochkristof/mlop-web`. Publish child commits first, then update the parent
submodule pins and merge the root repository change into `main` to deploy them.

## Secrets and access

- Production settings stay in `/opt/mlop/server/.env`, mode `0600`.
- `production` environment secret `LIGHTSAIL_DEPLOY_KEY` holds a dedicated SSH key.
  Its authorized key uses `restrict` and the forced command
  `/usr/local/bin/mlop-deploy-dispatch`. It cannot open a shell or forward ports.
- Environment variable `LIGHTSAIL_KNOWN_HOSTS` pins the host's Ed25519 key, verified
  over the existing administrative SSH connection.
- The production environment only permits `main`. No AWS credentials or runtime
  application secrets are stored in GitHub.
- Install `scripts/lightsail-dispatch.sh` root-owned, mode `0755`, at the forced
  command path. It validates the SHA against Theseus `main` before executing the
  deployment script from that commit. Changes to the dispatcher require an
  administrative install.

## Deployment and recovery

The dispatcher refuses dirty source checkouts. Deployment saves the prior SHA,
Compose file, application image tags and a compressed PostgreSQL dump under
`/home/ubuntu/.local/state/mlop-deploy/releases/` with private permissions. It then
checks out the pinned recursive source, builds applications serially, and starts
only `backend`, `frontend`, `ingest` and `py`. Compose project `server`, the existing
`.env`, data paths, databases, storage and Traefik are preserved.

Local and public HTTPS health probes must pass before `deployed-sha` is updated.
Failures restore the previous source and application images. Database migrations
are **not** automatically reversed. A schema failure may require an operator to
restore the saved database dump or deploy a forward fix.

Recovery metadata includes `previous-sha`, `compose.yml`, `images.yml` and
`postgres.sql.gz`. To restore saved images after checking out `previous-sha` and
updating submodules, use the relevant release directory:

```bash
sudo docker compose --project-name server \
  --project-directory /opt/mlop/server --env-file /opt/mlop/server/.env \
  -f /opt/mlop/server/docker-compose.yml -f "$release/images.yml" \
  up -d --no-deps --no-build --pull never backend frontend ingest py
```

At least 8 GiB of free disk is required before deployment. After a successful
deployment, the three newest recovery snapshots and their image tags are retained.
Inspect disk usage if a build exhausts space. Do not remove `.mlop` or run
`docker compose down -v`.
