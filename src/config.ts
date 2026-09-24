import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

import { Config, Project } from './types';

export const CONFIG_DIR = path.join(os.homedir(), '.config', 'supawake');

export const CONFIG_PATH = path.join(CONFIG_DIR, 'config.json');

/**
 * Every 4 hours, at minute 17.
 *
 * Supabase pauses a free project that sees too little user database activity
 * over a week; its docs say "a few user requests to the database each day" is
 * enough. The old default, one read every 3 days, fell short of that and
 * projects paused with every ping green. Minute 17 keeps runs off the top of
 * the hour, when scheduled traffic everywhere piles up.
 */
export const DEFAULT_INTERVAL = '17 */4 * * *';

const DEFAULT_CONFIG: Config = {
  projects: [],
  settings: {
    defaultInterval: DEFAULT_INTERVAL,
    notifications: {
      enabled: false,
      webhookUrl: '',
    },
  },
};

/**
 * Read an environment variable, tolerating the way container platforms
 * often store values.
 *
 * Coolify, docker-compose and .env files frequently keep the surrounding
 * quotes or a trailing newline as part of the value. An anon key carrying a
 * stray quote is rejected by Supabase with HTTP 401, which looks exactly
 * like a wrong key, so strip that noise before it reaches a header.
 */
function readEnvironmentValue(name: string): string | undefined {
  const raw = process.env[name];

  if (raw === undefined) {
    return undefined;
  }

  const trimmed = raw.trim().replace(/^(['"])([\s\S]*)\1$/, '$2').trim();

  return trimmed.length > 0 ? trimmed : undefined;
}

/**
 * Load Supabase projects from environment variables.
 *
 * Example:
 *
 * SUPABASE_1_NAME=discoursparfait
 * SUPABASE_1_URL=https://xxxxx.supabase.co
 * SUPABASE_1_KEY=eyJ...
 * SUPABASE_1_TABLE=keepalive
 *
 * SUPABASE_2_NAME=project2
 * SUPABASE_2_URL=https://yyyyy.supabase.co
 * SUPABASE_2_KEY=eyJ...
 *
 * SUPABASE_n_TABLE names an anon-readable table to select from. Without it
 * the ping falls back to the auth health endpoint, which answers without
 * touching Postgres and therefore does NOT prevent free-tier auto-pause.
 * SUPAWAKE_TABLE sets the table for every project that has no explicit one.
 *
 * SUPABASE_n_RPC names a heartbeat function that writes a row on each ping
 * (SUPAWAKE_RPC for all projects). SUPABASE_n_EMAIL / SUPABASE_n_PASSWORD sign
 * in as a real Auth user before querying. SUPABASE_n_REF overrides the project
 * ref for custom domains. SUPABASE_ACCESS_TOKEN enables auto-restore of paused
 * projects through the Management API.
 *
 * This is particularly useful when running Supawake
 * inside Docker / Coolify.
 */
function loadConfigFromEnvironment(): Config | null {
  const projects: Project[] = [];

  const environmentKeys = Object.keys(process.env);

  const defaultTable = readEnvironmentValue('SUPAWAKE_TABLE');

  const defaultRpc = readEnvironmentValue('SUPAWAKE_RPC');

  const projectNumbers = environmentKeys
    .map((key) => {
      const match = key.match(/^SUPABASE_(\d+)_(NAME|URL|KEY|TABLE|RPC|EMAIL|PASSWORD|REF)$/);

      if (!match) {
        return null;
      }

      return Number(match[1]);
    })
    .filter((value): value is number => value !== null);

  const uniqueProjectNumbers = [...new Set(projectNumbers)].sort(
    (a, b) => a - b,
  );

  for (const projectNumber of uniqueProjectNumbers) {
    const name =
      readEnvironmentValue(`SUPABASE_${projectNumber}_NAME`) ||
      `supabase-${projectNumber}`;

    const url = readEnvironmentValue(`SUPABASE_${projectNumber}_URL`);

    const anonKey = readEnvironmentValue(`SUPABASE_${projectNumber}_KEY`);

    const table =
      readEnvironmentValue(`SUPABASE_${projectNumber}_TABLE`) ??
      defaultTable;

    const rpc = readEnvironmentValue(`SUPABASE_${projectNumber}_RPC`) ?? defaultRpc;

    const email = readEnvironmentValue(`SUPABASE_${projectNumber}_EMAIL`);

    const password = readEnvironmentValue(`SUPABASE_${projectNumber}_PASSWORD`);

    const ref = readEnvironmentValue(`SUPABASE_${projectNumber}_REF`);

    if (Boolean(email) !== Boolean(password)) {
      throw new Error(
        `Incomplete sign-in for SUPABASE_${projectNumber}. Set both ` +
          `SUPABASE_${projectNumber}_EMAIL and SUPABASE_${projectNumber}_PASSWORD, or neither.`,
      );
    }

    if (!url || !anonKey) {
      throw new Error(
        `Incomplete configuration for SUPABASE_${projectNumber}. ` +
          `Both SUPABASE_${projectNumber}_URL and ` +
          `SUPABASE_${projectNumber}_KEY are required.`,
      );
    }

    projects.push({
      name,
      url,
      anonKey,
      ...(table ? { table } : {}),
      ...(rpc ? { rpc } : {}),
      ...(email && password ? { email, password } : {}),
      ...(ref ? { ref } : {}),
    });
  }

  if (projects.length === 0) {
    return null;
  }

  const webhookUrl = readEnvironmentValue('SUPAWAKE_WEBHOOK_URL');

  return {
    projects,
    settings: {
      defaultInterval:
        readEnvironmentValue('SUPAWAKE_INTERVAL') ||
        DEFAULT_CONFIG.settings.defaultInterval,

      /*
       * Environment mode had notifications hardcoded off, which made them
       * unreachable for every Docker / Coolify deployment.
       */
      notifications: {
        enabled: webhookUrl !== undefined,
        webhookUrl: webhookUrl ?? '',
      },

      accessToken: readEnvironmentValue('SUPABASE_ACCESS_TOKEN'),
    },
  };
}

/**
 * Whether Supawake is running from environment variables rather than from the
 * config file.
 *
 * Checking SUPABASE_1_URL alone was wrong: projects may legitimately start at
 * any number, so a container configured with only SUPABASE_2_* looked like CLI
 * mode and "supawake add" would write the environment-derived projects out to
 * disk.
 */
export function isEnvironmentConfigured(): boolean {
  return Object.keys(process.env).some((key) =>
    /^SUPABASE_\d+_(URL|KEY)$/.test(key),
  );
}

/**
 * Ensure the Supawake configuration directory exists.
 */
export function ensureConfigDir(): void {
  if (!fs.existsSync(CONFIG_DIR)) {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
  }
}

/**
 * Load configuration.
 *
 * Priority:
 *
 * 1. Environment variables
 * 2. ~/.config/supawake/config.json
 * 3. Default configuration
 */
export function loadConfig(): Config {
  /*
   * Docker / Coolify mode.
   *
   * If SUPABASE_1_URL, SUPABASE_1_KEY, etc.
   * are present, use them instead of the local config file.
   */
  const environmentConfig = loadConfigFromEnvironment();

  if (environmentConfig) {
    return environmentConfig;
  }

  /*
   * Normal Supawake CLI mode.
   *
   * Keep compatibility with the original application.
   */
  ensureConfigDir();

  if (!fs.existsSync(CONFIG_PATH)) {
    saveConfig(DEFAULT_CONFIG);

    return {
      ...DEFAULT_CONFIG,
      projects: [],
    };
  }

  try {
    const raw = fs.readFileSync(CONFIG_PATH, 'utf8');

    const parsed = JSON.parse(raw) as Partial<Config>;

    return {
      projects: parsed.projects ?? [],

      settings: {
        ...DEFAULT_CONFIG.settings,
        ...(parsed.settings ?? {}),

        // The token is a secret; letting the environment supply it keeps it
        // out of a config file that tends to get copied around.
        accessToken:
          readEnvironmentValue('SUPABASE_ACCESS_TOKEN') ?? parsed.settings?.accessToken,

        notifications: {
          ...DEFAULT_CONFIG.settings.notifications,
          ...(parsed.settings?.notifications ?? {}),
        },
      },
    };
  } catch (error) {
    throw new Error(
      `Failed to read Supawake configuration at ${CONFIG_PATH}: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
}

/**
 * Save configuration to the normal Supawake config file.
 *
 * This is kept for compatibility with the original CLI.
 */
export function saveConfig(config: Config): void {
  ensureConfigDir();

  fs.writeFileSync(
    CONFIG_PATH,
    JSON.stringify(config, null, 2) + '\n',
    'utf8',
  );
}

/**
 * Add a project to the local configuration.
 */
export function addProject(project: Project): Config {
  const config = loadConfig();

  if (config.projects.some((p) => p.name === project.name)) {
    throw new Error(`A project named "${project.name}" already exists.`);
  }

  config.projects.push(project);

  /*
   * If the configuration comes from environment variables,
   * don't attempt to overwrite it.
   */
  if (!isEnvironmentConfigured()) {
    saveConfig(config);
  }

  return config;
}

/**
 * Remove a project from the local configuration.
 */
export function removeProject(name: string): Config {
  const config = loadConfig();

  const before = config.projects.length;

  config.projects = config.projects.filter(
    (project) => project.name !== name,
  );

  if (config.projects.length === before) {
    throw new Error(`No project named "${name}" found.`);
  }

  /*
   * Environment based configurations are read-only.
   * They must be modified through Coolify.
   */
  if (!isEnvironmentConfigured()) {
    saveConfig(config);
  }

  return config;
}
