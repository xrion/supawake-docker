import chalk from 'chalk';
import { loadConfig } from '../config';
import { explainStatus, pingAll } from '../ping';
import { notifyFailures } from '../notify';
import { ensureAllAwake, ProjectState } from '../restore';
import { PingResult } from '../types';

/**
 * One line per project the Management API check had something to say about.
 * Healthy projects stay quiet: the ping line already covers them.
 */
export function formatState(state: ProjectState): string | undefined {
  const name = chalk.cyan(state.project.name.padEnd(20));
  if (state.error) {
    return `  ${chalk.yellow('⚠ CHECK')} ${name} ${chalk.yellow(state.error)}`;
  }
  if (state.restoreRequested) {
    return `  ${chalk.magenta('↻ WAKE ')} ${name} ${chalk.magenta(
      'was paused - restore requested, back in a few minutes',
    )}`;
  }
  if (state.status && state.status !== 'ACTIVE_HEALTHY') {
    return `  ${chalk.yellow('⚠ STATE')} ${name} ${chalk.yellow(state.status)}`;
  }
  return undefined;
}

/**
 * Ask Supabase to restore any paused project, when a Management API token is
 * configured, and print what it found.
 */
export async function wakePaused(
  projects: PingResult['project'][],
  accessToken: string | undefined,
): Promise<ProjectState[]> {
  const states = await ensureAllAwake(projects, accessToken);
  for (const state of states) {
    const line = formatState(state);
    if (line) console.log(line);
  }
  return states;
}

export function formatResult(r: PingResult): string {
  const tag = r.ok ? chalk.green('✓ OK  ') : chalk.red('✗ FAIL');
  const failure = r.error ?? [`HTTP ${r.status}`, r.detail].filter(Boolean).join(' — ');
  // Each request is listed, so it is visible that the sign-in and the write
  // actually happened rather than only the read.
  const steps = r.steps.map((s) => `${s.step} ${s.status ?? 'ERR'}`).join(', ');
  const failedStep = r.steps.find((s) => !s.ok)?.step;
  const detail = r.ok
    ? chalk.gray(`${steps} in ${r.durationMs}ms`)
    : chalk.red(`${failedStep ? `${failedStep}: ` : ''}${failure}`) + chalk.gray(` (${steps})`);
  const retried = r.attempts > r.steps.length ? chalk.yellow(` (${r.attempts} attempts)`) : '';
  // A pass against the auth fallback says nothing about the database, so never
  // let it render as an unqualified green tick.
  const shallow =
    r.project.table || r.project.rpc ? '' : chalk.yellow(' — auth only, DB not pinged');
  return `  ${tag} ${chalk.cyan(r.project.name.padEnd(20))} ${detail}${retried}${shallow}`;
}

export async function pingCommand(): Promise<void> {
  const config = loadConfig();
  if (config.projects.length === 0) {
    console.log(chalk.yellow('No projects configured. Run "supawake add" first.'));
    return;
  }
  console.log(chalk.bold(`Pinging ${config.projects.length} project(s)…`));
  await wakePaused(config.projects, config.settings.accessToken);
  const results = await pingAll(config.projects);
  for (const r of results) {
    console.log(formatResult(r));

    // A bare status leaves the operator guessing; name the thing to go and fix.
    for (const step of r.steps.filter((s) => !s.ok)) {
      const hint = explainStatus(step.status, r.project, step.step);
      if (hint) console.log(chalk.gray(`         ↳ ${step.step}: ${hint}`));
    }
  }

  await notifyFailures(results, config.settings.notifications);

  const failed = results.filter((r) => !r.ok).length;
  if (failed > 0) {
    console.log(chalk.red(`\n${failed} project(s) failed.`));
    process.exitCode = 1;
  } else {
    console.log(chalk.green(`\nAll projects alive.`));
  }

  const noTable = results
    .filter((r) => !r.project.table && !r.project.rpc)
    .map((r) => r.project.name);
  if (noTable.length > 0) {
    console.log(
      chalk.yellow(
        `\nWarning: ${noTable.join(', ')} have no "table" configured, so their pings never reach\n` +
          `Postgres and will not prevent auto-pause. Set one with "supawake add", by adding\n` +
          `"table": "keepalive" to the project in your config, or — when running in Docker or\n` +
          `Coolify — with SUPABASE_<n>_TABLE=keepalive (or SUPAWAKE_TABLE=keepalive for all).`,
      ),
    );
  }
}
