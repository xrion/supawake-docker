import chalk from 'chalk';
import cron from 'node-cron';
import { loadConfig } from '../config';
import { pingAll } from '../ping';
import { notifyFailures } from '../notify';
import { formatResult } from './ping';

export async function startCommand(opts: { interval?: string }): Promise<void> {
  const config = loadConfig();
  const schedule = opts.interval || config.settings.defaultInterval;

  if (!cron.validate(schedule)) {
    console.error(chalk.red(`✗ Invalid cron expression: "${schedule}"`));
    process.exit(1);
  }

  if (config.projects.length === 0) {
    /*
     * Exit non-zero. Returning quietly made the container exit 0, which reads
     * as "finished successfully" to Docker, Coolify and systemd - they restart
     * it forever with nothing in the logs explaining why nothing is pinged.
     */
    console.error(
      chalk.red(
        '\u2717 No projects configured. Set SUPABASE_1_URL and SUPABASE_1_KEY\n' +
          '  (plus SUPABASE_1_TABLE) in the environment, or run "supawake add".',
      ),
    );
    process.exit(1);
  }

  const untabled = config.projects.filter((project) => !project.table);
  if (untabled.length > 0) {
    console.log(
      chalk.yellow(
        `\u26a0 ${untabled.map((p) => p.name).join(', ')}: no table configured - ` +
          'pings will stop at the auth endpoint and will NOT prevent auto-pause.',
      ),
    );
  }

  console.log(chalk.bold(`supawake started`));
  console.log(chalk.gray(`  schedule: ${schedule}`));
  console.log(chalk.gray(`  projects: ${config.projects.length}`));
  console.log(chalk.gray(`  press Ctrl+C to stop\n`));

  const run = async () => {
    const ts = new Date().toISOString();
    console.log(chalk.bold(`[${ts}] pinging…`));
    const latest = loadConfig();
    const results = await pingAll(latest.projects);
    for (const r of results) console.log(formatResult(r));
    await notifyFailures(results, latest.settings.notifications);
    console.log('');
  };

  // Run once immediately so users get instant feedback.
  await run();

  const task = cron.schedule(schedule, run);
  task.start();

  const stop = () => {
    console.log(chalk.yellow('\nStopping…'));
    task.stop();
    process.exit(0);
  };
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
}
