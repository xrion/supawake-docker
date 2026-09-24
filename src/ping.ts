import { PingResult, Project, StepResult } from './types';

/**
 * Backoff before each retry.
 *
 * Transient DNS blips and cold edge nodes resolve well within a few seconds;
 * anything longer is a real outage and should be reported as one.
 */
const RETRY_DELAYS_MS = [500, 2000];

/**
 * A table read goes through PostgREST to Postgres, which counts as database
 * activity.
 *
 * The auth health endpoint is only a fallback for projects with no table
 * configured: it proves the project answers, but generates no database
 * activity.
 */
export function pingUrl(project: Project): string {
  const base = project.url.replace(/\/$/, '');

  if (!project.table) {
    return `${base}/auth/v1/health`;
  }

  return `${base}/rest/v1/${encodeURIComponent(
    project.table,
  )}?select=*&limit=1`;
}

/**
 * PostgREST and GoTrue both answer failures with a small JSON body naming the
 * real cause. Surfacing it turns an opaque "HTTP 401" into something the
 * operator can act on.
 */
async function readFailureDetail(res: Response): Promise<string | undefined> {
  try {
    const body = (await res.text()).trim();

    if (!body) {
      return undefined;
    }

    try {
      const parsed = JSON.parse(body) as {
        message?: string;
        msg?: string;
        error?: string;
        error_description?: string;
        hint?: string;
        code?: string;
      };

      const message =
        parsed.message ??
        parsed.msg ??
        parsed.error_description ??
        parsed.error ??
        parsed.hint;

      if (message) {
        return parsed.code ? `${parsed.code}: ${message}` : message;
      }
    } catch {
      /* Not JSON - fall through to the raw body. */
    }

    return body.slice(0, 200);
  } catch {
    return undefined;
  }
}

/**
 * Turn a failing status into the concrete thing to go and fix.
 */
export function explainStatus(
  status: number | undefined,
  project: Project,
  step = 'read',
): string | undefined {
  if (step === 'sign-in') {
    if (status === 400) {
      return 'sign-in refused - check SUPABASE_n_EMAIL / SUPABASE_n_PASSWORD, and that the user exists and its email is confirmed';
    }
    if (status === 429) {
      return 'Supabase Auth rate-limited the sign-in - ping less often';
    }
  }

  if (step === 'rpc') {
    if (status === 404) {
      return `function "${project.rpc}" was not found - create it (see supabase-sql-config.md), then run ` +
        "notify pgrst, 'reload schema';";
    }
    if (status === 401 || status === 403) {
      return `the caller may not execute "${project.rpc}" - grant execute on it to anon and authenticated`;
    }
  }

  if (status === 401) {
    return isPublishableKey(project.anonKey)
      ? 'the publishable key was rejected - check it belongs to this project and has not been revoked'
      : 'the key was rejected - a legacy anon key must be the project\'s "anon public" JWT, not the service_role or a stale key';
  }

  if (status === 403) {
    return project.table
      ? `the caller may not select from "${project.table}" - grant it a row-level security select policy`
      : 'the project refused the request';
  }

  if (status === 404) {
    return project.table
      ? `table "${project.table}" was not found - create it, or fix SUPABASE_n_TABLE. ` +
          'If you just created it, reload the PostgREST schema cache with ' +
          "notify pgrst, 'reload schema';"
      : 'the endpoint was not found - check the project URL';
  }

  if (status === 400) {
    return project.table
      ? `PostgREST rejected the query against "${project.table}" - check the table name and that the public schema is exposed`
      : 'the request was rejected - check the project URL';
  }

  if (status !== undefined && status >= 500) {
    return 'Supabase answered with a server error - the project may be paused, restoring, or over its limits; check the dashboard';
  }

  return undefined;
}

/**
 * Worth trying again:
 * network failures, timeouts, and upstream 5xx.
 *
 * Authentication failures such as 401/403 are not retried because
 * retrying the same invalid credentials will not fix the problem.
 */
function isTransient(attempt: Attempt): boolean {
  if (attempt.error !== undefined) {
    return true;
  }

  return attempt.status !== undefined && attempt.status >= 500;
}

/**
 * Determine whether a Supabase key is a new publishable key.
 *
 * New Supabase keys look like:
 *
 *   sb_publishable_...
 *
 * Legacy anon keys are JWTs and usually look like:
 *
 *   eyJ...
 */
function isPublishableKey(key: string): boolean {
  return key.startsWith('sb_publishable_');
}

interface Request {
  method: 'GET' | 'POST';
  url: string;
  headers: Record<string, string>;
  body?: unknown;
}

interface Attempt {
  ok: boolean;
  status?: number;
  error?: string;
  detail?: string;
  /** Parsed response body, kept only for successful requests that return one. */
  json?: unknown;
}

async function attemptRequest(
  request: Request,
  timeoutMs: number,
): Promise<Attempt> {
  const controller = new AbortController();

  const timer = setTimeout(() => {
    controller.abort();
  }, timeoutMs);

  try {
    const res = await fetch(request.url, {
      method: request.method,
      headers:
        request.body === undefined
          ? request.headers
          : { ...request.headers, 'Content-Type': 'application/json' },
      body: request.body === undefined ? undefined : JSON.stringify(request.body),
      signal: controller.signal,
    });

    /*
     * Any 2xx means the request reached its service and succeeded.
     *
     * It must NOT be narrowed to 200: PostgREST answers a range-limited read
     * such as "?select=*&limit=1" with 206 Partial Content whenever the page
     * it returned may not be the whole collection, and sign-out answers 204.
     */
    if (res.ok) {
      let json: unknown;
      try {
        const text = await res.text();
        json = text ? JSON.parse(text) : undefined;
      } catch {
        json = undefined;
      }
      return { ok: true, status: res.status, json };
    }

    return {
      ok: false,
      status: res.status,
      detail: await readFailureDetail(res),
    };
  } catch (err) {
    return {
      ok: false,
      error: (err as Error).message,
    };
  } finally {
    clearTimeout(timer);
  }
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

async function runStep(
  step: string,
  request: Request,
  timeoutMs: number,
): Promise<StepResult & { json?: unknown }> {
  let attempt: Attempt = { ok: false, error: 'no attempt made' };

  for (let i = 0; i <= RETRY_DELAYS_MS.length; i++) {
    attempt = await attemptRequest(request, timeoutMs);

    if (attempt.ok || !isTransient(attempt) || i === RETRY_DELAYS_MS.length) {
      return { step, ...attempt, attempts: i + 1 };
    }

    await sleep(RETRY_DELAYS_MS[i]);
  }

  /* istanbul ignore next - loop always returns */
  return { step, ...attempt, attempts: RETRY_DELAYS_MS.length + 1 };
}

/**
 * Headers for a PostgREST or GoTrue call.
 *
 * Both legacy anon keys and new publishable keys go in the apikey header.
 * The Authorization header carries the signed-in user's access token when
 * there is one, which makes Postgres run the query as "authenticated".
 * Without a user, legacy anon JWTs are also sent as the bearer token; new
 * sb_publishable_* keys are opaque, NOT JWTs, and sending one as a bearer
 * token results in HTTP 401.
 */
function headersFor(
  project: Project,
  accessToken?: string,
): Record<string, string> {
  const headers: Record<string, string> = { apikey: project.anonKey };

  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken}`;
  } else if (!isPublishableKey(project.anonKey)) {
    headers.Authorization = `Bearer ${project.anonKey}`;
  }

  return headers;
}

/**
 * Generate one burst of activity for a project.
 *
 * Supabase pauses a free project when it sees too little *user* database
 * activity over a week ("a few user requests to the database each day"), so
 * a single read every few days is not enough. Each ping therefore sends every
 * kind of traffic the project is configured for:
 *
 *   1. sign-in   - a password login, as a real visitor would (writes to
 *                  auth.sessions, auth.refresh_tokens and last_sign_in_at)
 *   2. read      - a PostgREST select against the keepalive table
 *   3. rpc       - a heartbeat function that writes a row
 *   4. sign-out  - ends the session created in step 1
 *
 * A failed sign-in does not stop the rest: the read and RPC still run as anon,
 * so the database keeps getting traffic while the credentials get fixed.
 */
export async function pingProject(
  project: Project,
  timeoutMs = 15000,
): Promise<PingResult> {
  const start = Date.now();
  const base = project.url.replace(/\/$/, '');
  const steps: StepResult[] = [];
  let accessToken: string | undefined;

  const record = (result: StepResult & { json?: unknown }): unknown => {
    const { json, ...step } = result;
    steps.push(step);
    return json;
  };

  if (project.email && project.password) {
    const json = record(
      await runStep(
        'sign-in',
        {
          method: 'POST',
          url: `${base}/auth/v1/token?grant_type=password`,
          headers: headersFor(project),
          body: { email: project.email, password: project.password },
        },
        timeoutMs,
      ),
    ) as { access_token?: string } | undefined;

    accessToken = json?.access_token;
  }

  if (project.table || !project.rpc) {
    record(
      await runStep(
        project.table ? 'read' : 'health',
        { method: 'GET', url: pingUrl(project), headers: headersFor(project, accessToken) },
        timeoutMs,
      ),
    );
  }

  if (project.rpc) {
    record(
      await runStep(
        'rpc',
        {
          method: 'POST',
          url: `${base}/rest/v1/rpc/${encodeURIComponent(project.rpc)}`,
          headers: headersFor(project, accessToken),
          body: {},
        },
        timeoutMs,
      ),
    );
  }

  if (accessToken) {
    // scope=local ends only this session, not the user's sessions elsewhere.
    record(
      await runStep(
        'sign-out',
        {
          method: 'POST',
          url: `${base}/auth/v1/logout?scope=local`,
          headers: headersFor(project, accessToken),
        },
        timeoutMs,
      ),
    );
  }

  const failed = steps.find((step) => !step.ok);
  const summary = failed ?? steps[steps.length - 1];

  return {
    project,
    steps,
    ok: failed === undefined,
    status: summary.status,
    error: summary.error,
    detail: summary.detail,
    attempts: steps.reduce((total, step) => total + step.attempts, 0),
    durationMs: Date.now() - start,
  };
}

export async function pingAll(
  projects: Project[],
): Promise<PingResult[]> {
  return Promise.all(projects.map((project) => pingProject(project)));
}
