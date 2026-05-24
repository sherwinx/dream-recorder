import requests

from functions.config_loader import get_config


class DayOneSyncError(RuntimeError):
    pass


def is_dayone_sync_enabled(config=None):
    config = config or get_config()
    return str(config.get('DAYONE_SYNC_ENABLED', False)).lower() in ('1', 'true', 'yes')


def build_cloud_job_payload(job, config=None):
    config = config or get_config()
    return {
        'idempotency_key': job['idempotency_key'],
        'device_id': config.get('DAYONE_DEVICE_ID', 'dream-recorder'),
        'transcript': job['transcript'],
        'dream_local_date': job['dream_local_date'],
        'dream_local_time': job['dream_local_time'],
        'audio_filename': job.get('audio_filename'),
    }


def submit_dayone_job(job, config=None, session=None):
    config = config or get_config()
    relay_url = (config.get('DAYONE_RELAY_URL') or '').rstrip('/')
    token = config.get('DAYONE_RELAY_TOKEN')
    if not relay_url:
        raise DayOneSyncError("DAYONE_RELAY_URL is required when Day One sync is enabled")
    if not token:
        raise DayOneSyncError("DAYONE_RELAY_TOKEN is required when Day One sync is enabled")

    http = session or requests
    response = http.post(
        f"{relay_url}/api/jobs",
        json=build_cloud_job_payload(job, config=config),
        headers={
            'Authorization': f"Bearer {token}",
            'User-Agent': 'dream-recorder-pi/1.0',
        },
        timeout=float(config.get('DAYONE_RELAY_TIMEOUT_SECONDS', 10)),
    )
    response.raise_for_status()
    return response.json()


def submit_pending_dayone_sync_jobs(dream_db, config=None, logger=None, limit=10, session=None):
    """Submit locally pending Day One jobs to the Cloudflare relay."""
    config = config or get_config()
    if not is_dayone_sync_enabled(config):
        return {'submitted': 0, 'failed': 0, 'skipped': True}

    submitted = 0
    failed = 0
    jobs = dream_db.get_dayone_sync_jobs(statuses=('pending',), limit=limit)
    for job in jobs:
        try:
            result = submit_dayone_job(job, config=config, session=session)
            relay_job = result.get('job') or result
            relay_job_id = relay_job.get('id') if isinstance(relay_job, dict) else None
            dream_db.mark_dayone_sync_submitted(job['id'], relay_job_id=relay_job_id)
            submitted += 1
        except Exception as exc:
            dream_db.mark_dayone_sync_failed(job['id'], str(exc))
            failed += 1
            if logger:
                logger.error(f"Failed to submit Day One sync job {job['id']}: {exc}")

    return {'submitted': submitted, 'failed': failed, 'skipped': False}
