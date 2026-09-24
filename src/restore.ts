import { Project } from './types';

const MANAGEMENT_API = 'https://api.supabase.com/v1';

export interface ProjectState {
  project: Project;
  /** Supabase's status string, e.g. ACTIVE_HEALTHY, INACTIVE, RESTORING. */
  status?: string;
  /** True when supawake asked Supabase to restore the project on this run. */
  restoreRequested: boolean;
  error?: string;
}

/**
 * The Management API addresses projects by ref, which is the subdomain of a
 * standard https://<ref>.supabase.co URL. Custom domains carry no ref, so
 * those projects need it set explicitly.
 */
export function projectRef(project: Project): string | undefined {
  if (project.ref) {
    return project.ref;
  }

  const match = project.url.match(/^https?:\/\/([a-z0-9]+)\.supabase\.co\/?$/i);
  return match ? match[1].toLowerCase() : undefined;
}

async function managementRequest(
  method: 'GET' | 'POST',
  path: string,
  accessToken: string,
): Promise<{ status: number; json?: unknown; text: string }> {
  const res = await fetch(`${MANAGEMENT_API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${accessToken}` },
    signal: AbortSignal.timeout(15000),
  });
  const text = await res.text();
  let json: unknown;
  try {
    json = text ? JSON.parse(text) : undefined;
  } catch {
    json = undefined;
  }
  return { status: res.status, json, text };
}

/**
 * Read a project's status and, when Supabase has already paused it, ask for
 * it to be restored.
 *
 * Pings cannot wake a paused project - its database is shut down and the
 * REST endpoint just errors - so without this a single missed week means a
 * manual trip to the dashboard. Restoring takes a few minutes; the pings on
 * this run will fail and the next run finds the project healthy again.
 */
export async function ensureAwake(project: Project, accessToken: string): Promise<ProjectState> {
  const ref = projectRef(project);

  if (!ref) {
    return {
      project,
      restoreRequested: false,
      error: 'cannot derive the project ref from a custom-domain URL - set SUPABASE_n_REF',
    };
  }

  try {
    const info = await managementRequest('GET', `/projects/${ref}`, accessToken);

    if (info.status !== 200) {
      return {
        project,
        restoreRequested: false,
        error:
          info.status === 401
            ? 'Management API rejected SUPABASE_ACCESS_TOKEN - create a new personal access token'
            : `Management API answered HTTP ${info.status} for ${ref}`,
      };
    }

    const status = (info.json as { status?: string } | undefined)?.status;

    if (status !== 'INACTIVE') {
      return { project, status, restoreRequested: false };
    }

    const restore = await managementRequest('POST', `/projects/${ref}/restore`, accessToken);

    if (restore.status >= 200 && restore.status < 300) {
      return { project, status, restoreRequested: true };
    }

    return {
      project,
      status,
      restoreRequested: false,
      error: `restore refused (HTTP ${restore.status}): ${restore.text.slice(0, 200)}`,
    };
  } catch (err) {
    return { project, restoreRequested: false, error: (err as Error).message };
  }
}

export async function ensureAllAwake(
  projects: Project[],
  accessToken: string | undefined,
): Promise<ProjectState[]> {
  if (!accessToken) {
    return [];
  }
  return Promise.all(projects.map((project) => ensureAwake(project, accessToken)));
}
