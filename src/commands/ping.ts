import chalk from 'chalk';
import { loadConfig } from '../config';
import { explainStatus, pingAll } from '../ping';
import { notifyFailures } from '../notify';
import { PingResult } from '../types';

export function formatResult(r: PingResult): string {
  const tag = r.ok ? chalk.green('✓ OK  ') : chalk.red('✗ FAIL');
  const failure = r.error ?? [`HTTP ${r.status}`, r.detail].filter(Boolean).join(' — ');
  const detail = r.ok ? chalk.gray(`${r.status} in ${r.durationMs}ms`) : chalk.red(failure);
  const retried = r.attempts > 1 ? chalk.yellow(` (${r.attempts} attempts)`) : '';
  // A pass against the auth fallback says nothing about the database, so never
  // let it render as an unqualified green tick.
  const shallow = r.project.table ? '' : chalk.yellow(' — auth only, DB not pinged');
  return `  ${tag} ${chalk.cyan(r.project.name.padEnd(20))} ${detail}${retried}${shallow}`;
}

export async function pingCommand(): Promise<void> {
  const config = loadConfig();
  if (config.projects.length === 0) {
    console.log(chalk.yellow('No projects configured. Run "supawake add" first.'));
    return;
  }
  console.log(chalk.bold(`Pinging ${config.projects.length} project(s)…`));
  const results = await pingAll(config.projects);
  for (const r of results) {
    console.log(formatResult(r));

    // A bare status leaves the operator guessing; name the thing to go and fix.
    const hint = r.ok ? undefined : explainStatus(r.status, r.project);
    if (hint) console.log(chalk.gray(`         ↳ ${hint}`));
  }

  await notifyFailures(results, config.settings.notifications);

  const failed = results.filter((r) => !r.ok).length;
  if (failed > 0) {
    console.log(chalk.red(`\n${failed} project(s) failed.`));
    process.exitCode = 1;
  } else {
    console.log(chalk.green(`\nAll projects alive.`));
  }

  const noTable = results.filter((r) => !r.project.table).map((r) => r.project.name);
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
