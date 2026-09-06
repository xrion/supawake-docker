import { PingResult, Project } from './types';

/**
 * Backoff before each retry.
 *
 * Transient DNS blips and cold edge nodes resolve well within a few seconds;
 * anything longer is a real outage and should be reported as one.
 */
const RETRY_DELAYS_MS = [500, 2000];

/**
 * A table read goes through PostgREST to Postgres, which is what keeps a
 * free-tier project from auto-pausing.
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

interface Attempt {
  ok: boolean;
  status?: number;
  error?: string;
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

async function attemptPing(
  project: Project,
  timeoutMs: number,
): Promise<Attempt> {
  const controller = new AbortController();

  const timer = setTimeout(() => {
    controller.abort();
  }, timeoutMs);

  try {
    const publishableKey = isPublishableKey(project.anonKey);

    const headers: Record<string, string> = {
      /*
       * Both legacy anon keys and new publishable keys are sent
       * through the apikey header.
       */
      apikey: project.anonKey,
    };

    /*
     * Legacy anon keys are JWTs.
     *
     * The old Supabase API expected the JWT to also be supplied
     * as the Authorization Bearer token.
     *
     * New sb_publishable_* keys are opaque API keys, NOT JWTs,
     * so we must NOT send:
     *
     * Authorization: Bearer sb_publishable_...
     *
     * because that can result in HTTP 401.
     */
    if (!publishableKey) {
      headers.Authorization = `Bearer ${project.anonKey}`;
    }

    const res = await fetch(pingUrl(project), {
      method: 'GET',
      headers,
      signal: controller.signal,
    });

    return {
      ok: res.status === 200,
      status: res.status,
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

export async function pingProject(
  project: Project,
  timeoutMs = 15000,
): Promise<PingResult> {
  const start = Date.now();

  let attempt: Attempt = {
    ok: false,
    error: 'no attempt made',
  };

  for (let i = 0; i <= RETRY_DELAYS_MS.length; i++) {
    attempt = await attemptPing(project, timeoutMs);

    if (
      attempt.ok ||
      !isTransient(attempt) ||
      i === RETRY_DELAYS_MS.length
    ) {
      return {
        project,
        ...attempt,
        attempts: i + 1,
        durationMs: Date.now() - start,
      };
    }

    await sleep(RETRY_DELAYS_MS[i]);
  }

  /* istanbul ignore next - loop always returns */
  return {
    project,
    ...attempt,
    attempts: RETRY_DELAYS_MS.length + 1,
    durationMs: Date.now() - start,
  };
}

export async function pingAll(
  projects: Project[],
): Promise<PingResult[]> {
  return Promise.all(projects.map((project) => pingProject(project)));
}
