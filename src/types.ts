export interface Project {
  name: string;
  url: string;
  anonKey: string;
  /**
   * Table to read when pinging, e.g. "keepalive". When set, the ping issues a
   * PostgREST select against it, which reaches Postgres and therefore counts as
   * database activity. When unset, the ping falls back to the auth health
   * endpoint, which confirms the project responds but does NOT touch the
   * database and so will not prevent free-tier auto-pause.
   */
  table?: string;
  /**
   * Postgres function called through PostgREST on each ping, e.g.
   * "supawake_heartbeat". It performs a small write (update + insert + prune),
   * which is a stronger activity signal than a read alone.
   */
  rpc?: string;
  /**
   * Credentials of a dedicated Supabase Auth user. When both are set, each ping
   * signs in as that user first, runs the table read and RPC as the
   * "authenticated" role, then signs out - the same traffic a real visitor
   * logging into the app generates.
   */
  email?: string;
  password?: string;
  /**
   * Project ref used for the Management API. Derived from a
   * https://<ref>.supabase.co URL when unset; needed only for custom domains.
   */
  ref?: string;
}

export interface NotificationSettings {
  enabled: boolean;
  webhookUrl: string;
}

export interface Settings {
  defaultInterval: string;
  notifications: NotificationSettings;
  /**
   * Supabase personal access token (Management API). When set, supawake checks
   * each project's status before pinging and asks Supabase to restore any that
   * are paused.
   */
  accessToken?: string;
}

export interface Config {
  projects: Project[];
  settings: Settings;
}

export interface StepResult {
  /** "sign-in", "read", "rpc" or "sign-out" - or "health" for the auth fallback. */
  step: string;
  ok: boolean;
  status?: number;
  error?: string;
  detail?: string;
  attempts: number;
}

export interface PingResult {
  project: Project;
  /** Every request sent for this project, in order. */
  steps: StepResult[];
  ok: boolean;
  status?: number;
  error?: string;
  /** Message the server sent back with a failing status, when it sent one. */
  detail?: string;
  durationMs: number;
  /** How many requests were sent, including retries. 1 means it worked first try. */
  attempts: number;
}
