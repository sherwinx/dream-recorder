# Dream Recorder Day One Sync Relay

Cloudflare Worker + D1 relay for backlog-safe Day One sync.

## Deploy

1. Create a D1 database:
   ```sh
   wrangler d1 create dream-recorder-dayone-sync
   ```

2. Copy the example config and set the returned D1 `database_id`:
   ```sh
   cp wrangler.example.toml wrangler.toml
   ```

3. Apply the migration:
   ```sh
   wrangler d1 migrations apply dream-recorder-dayone-sync
   ```

4. Set secrets:
   ```sh
   wrangler secret put PI_SUBMIT_TOKEN
   wrangler secret put MAC_WORKER_TOKEN
   ```

5. Deploy:
   ```sh
   wrangler deploy
   ```

## API

- `POST /api/jobs` with `Authorization: Bearer <PI_SUBMIT_TOKEN>`
- `GET /api/jobs/pending` with `Authorization: Bearer <MAC_WORKER_TOKEN>`
- `POST /api/jobs/:id/complete` with `Authorization: Bearer <MAC_WORKER_TOKEN>`
- `POST /api/jobs/:id/fail` with `Authorization: Bearer <MAC_WORKER_TOKEN>`
