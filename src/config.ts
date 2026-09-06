import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

import { Config, Project } from './types';

export const CONFIG_DIR = path.join(os.homedir(), '.config', 'supawake');

export const CONFIG_PATH = path.join(CONFIG_DIR, 'config.json');

const DEFAULT_CONFIG: Config = {
  projects: [],
  settings: {
    defaultInterval: '0 0 */3 * *',
    notifications: {
      enabled: false,
      webhookUrl: '',
    },
  },
};

/**
 * Load Supabase projects from environment variables.
 *
 * Example:
 *
 * SUPABASE_1_NAME=discoursparfait
 * SUPABASE_1_URL=https://xxxxx.supabase.co
 * SUPABASE_1_KEY=eyJ...
 *
 * SUPABASE_2_NAME=project2
 * SUPABASE_2_URL=https://yyyyy.supabase.co
 * SUPABASE_2_KEY=eyJ...
 *
 * This is particularly useful when running Supawake
 * inside Docker / Coolify.
 */
function loadConfigFromEnvironment(): Config | null {
  const projects: Project[] = [];

  const environmentKeys = Object.keys(process.env);

  const projectNumbers = environmentKeys
    .map((key) => {
      const match = key.match(/^SUPABASE_(\d+)_(NAME|URL|KEY)$/);

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
      process.env[`SUPABASE_${projectNumber}_NAME`] ||
      `supabase-${projectNumber}`;

    const url = process.env[`SUPABASE_${projectNumber}_URL`];

    const anonKey = process.env[`SUPABASE_${projectNumber}_KEY`];

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
    });
  }

  if (projects.length === 0) {
    return null;
  }

  return {
    projects,
    settings: {
      defaultInterval:
        process.env.SUPAWAKE_INTERVAL ||
        DEFAULT_CONFIG.settings.defaultInterval,

      notifications: {
        enabled: false,
        webhookUrl: '',
      },
    },
  };
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
  if (!process.env.SUPABASE_1_URL) {
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
  if (!process.env.SUPABASE_1_URL) {
    saveConfig(config);
  }

  return config;
}
