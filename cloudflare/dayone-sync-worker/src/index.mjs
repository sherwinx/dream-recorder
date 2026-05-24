const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: JSON_HEADERS,
  });
}

function unauthorized() {
  return jsonResponse({ error: "unauthorized" }, 401);
}

function bearerToken(request) {
  const header = request.headers.get("authorization") || "";
  const match = header.match(/^Bearer\s+(.+)$/i);
  return match ? match[1] : null;
}

function requireToken(request, expectedToken) {
  return expectedToken && bearerToken(request) === expectedToken;
}

async function readJson(request) {
  try {
    return await request.json();
  } catch (_error) {
    return null;
  }
}

function validateJobPayload(payload) {
  const required = [
    "idempotency_key",
    "device_id",
    "transcript",
    "dream_local_date",
    "dream_local_time",
  ];
  for (const field of required) {
    if (!payload || typeof payload[field] !== "string" || payload[field].trim() === "") {
      return `${field} is required`;
    }
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(payload.dream_local_date)) {
    return "dream_local_date must use YYYY-MM-DD";
  }
  if (!/^\d{2}:\d{2}$/.test(payload.dream_local_time)) {
    return "dream_local_time must use HH:MM";
  }
  return null;
}

async function getJobByIdempotencyKey(db, key) {
  return db
    .prepare("SELECT * FROM dayone_sync_jobs WHERE idempotency_key = ?")
    .bind(key)
    .first();
}

async function getJobById(db, id) {
  return db.prepare("SELECT * FROM dayone_sync_jobs WHERE id = ?").bind(id).first();
}

async function createJob(request, env) {
  if (!requireToken(request, env.PI_SUBMIT_TOKEN)) {
    return unauthorized();
  }

  const payload = await readJson(request);
  const validationError = validateJobPayload(payload);
  if (validationError) {
    return jsonResponse({ error: validationError }, 400);
  }

  const existing = await getJobByIdempotencyKey(env.DB, payload.idempotency_key);
  if (existing) {
    return jsonResponse({ job: existing, duplicate: true });
  }

  await env.DB.prepare(
    `INSERT INTO dayone_sync_jobs (
      idempotency_key,
      device_id,
      transcript,
      dream_local_date,
      dream_local_time,
      audio_filename,
      status
    ) VALUES (?, ?, ?, ?, ?, ?, 'pending')`
  )
    .bind(
      payload.idempotency_key,
      payload.device_id,
      payload.transcript,
      payload.dream_local_date,
      payload.dream_local_time,
      payload.audio_filename || null
    )
    .run();

  const job = await getJobByIdempotencyKey(env.DB, payload.idempotency_key);
  return jsonResponse({ job, duplicate: false }, 201);
}

async function pendingJobs(request, env) {
  if (!requireToken(request, env.MAC_WORKER_TOKEN)) {
    return unauthorized();
  }

  const url = new URL(request.url);
  const limit = Number.parseInt(url.searchParams.get("limit") || "0", 10);
  const query = `
    SELECT *
    FROM dayone_sync_jobs
    WHERE status != 'complete'
    ORDER BY dream_local_date, dream_local_time, id
  `;
  const statement = limit > 0
    ? env.DB.prepare(`${query} LIMIT ?`).bind(limit)
    : env.DB.prepare(query);
  const { results } = await statement.all();
  return jsonResponse({ jobs: results || [] });
}

async function completeJob(request, env, jobId) {
  if (!requireToken(request, env.MAC_WORKER_TOKEN)) {
    return unauthorized();
  }

  const payload = (await readJson(request)) || {};
  const existing = await getJobById(env.DB, jobId);
  if (!existing) {
    return jsonResponse({ error: "job not found" }, 404);
  }

  await env.DB.prepare(
    `UPDATE dayone_sync_jobs
     SET status = 'complete',
         dayone_entry_id = ?,
         completed_at = CURRENT_TIMESTAMP,
         last_error = NULL,
         updated_at = CURRENT_TIMESTAMP
     WHERE id = ?`
  )
    .bind(payload.dayone_entry_id || null, jobId)
    .run();

  return jsonResponse({ job: await getJobById(env.DB, jobId) });
}

async function failJob(request, env, jobId) {
  if (!requireToken(request, env.MAC_WORKER_TOKEN)) {
    return unauthorized();
  }

  const payload = (await readJson(request)) || {};
  const existing = await getJobById(env.DB, jobId);
  if (!existing) {
    return jsonResponse({ error: "job not found" }, 404);
  }

  await env.DB.prepare(
    `UPDATE dayone_sync_jobs
     SET status = 'failed',
         attempts = attempts + 1,
         last_error = ?,
         updated_at = CURRENT_TIMESTAMP
     WHERE id = ?`
  )
    .bind(String(payload.error || "unknown error"), jobId)
    .run();

  return jsonResponse({ job: await getJobById(env.DB, jobId) });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const method = request.method.toUpperCase();

    if (method === "GET" && url.pathname === "/health") {
      return jsonResponse({ ok: true });
    }

    if (method === "POST" && url.pathname === "/api/jobs") {
      return createJob(request, env);
    }

    if (method === "GET" && url.pathname === "/api/jobs/pending") {
      return pendingJobs(request, env);
    }

    const completeMatch = url.pathname.match(/^\/api\/jobs\/(\d+)\/complete$/);
    if (method === "POST" && completeMatch) {
      return completeJob(request, env, completeMatch[1]);
    }

    const failMatch = url.pathname.match(/^\/api\/jobs\/(\d+)\/fail$/);
    if (method === "POST" && failMatch) {
      return failJob(request, env, failMatch[1]);
    }

    return jsonResponse({ error: "not found" }, 404);
  },
};
