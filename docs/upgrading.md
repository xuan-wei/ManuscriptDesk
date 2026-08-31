# Upgrading

Treat every upgrade as an application image plus schema transition. Read `CHANGELOG.md` and the release notes before proceeding.

## Before upgrading

1. Confirm the target release supports the current database schema.
2. Build or pull the target image without changing the running service.
3. Create a paired database and SecretStore backup; retain the current long-term keys and Compose configuration.
4. Rehearse the upgrade on a restored copy when the release changes schema.
5. Confirm only one scheduler will run against the state set.

## Upgrade

From the private deployment directory:

```bash
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8008/api/health
```

Application startup performs supported forward migrations. Do not interrupt a migration. After startup, verify health, login, core manuscript views, journal configuration, Shortcut links, and automation configuration before admitting normal traffic.

## Version 0.1.0 schema note

This release uses Schema v8 and can migrate a supported Schema v7 database forward. The v7-to-v8 migration adds automation claim fencing metadata and requeues stale claimed/running requests. Always rehearse against a WAL-consistent backup plus matching SecretStore.

The runtime currently uses a long automation lease while provider work executes. Claim-token fencing prevents stale workers from finishing a reclaimed run, but periodic heartbeat during a long provider execution remains future hardening. Keep `WEB_CONCURRENCY=1` and do not run concurrent production schedulers.

## Rollback

Schema migrations are not automatically reversible. Never start the old image against the newly migrated database. If rollback is necessary:

1. Remove traffic from and stop the new service.
2. Preserve its state for diagnosis.
3. Restore the pre-upgrade database and matching SecretStore pair.
4. Restore the matching long-term keys, old image, and old deployment configuration.
5. Start the old service and verify health and login.

Any writes accepted after the upgrade will not exist in the pre-upgrade backup; plan a controlled maintenance window for schema-changing releases.
