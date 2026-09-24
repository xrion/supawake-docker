import chalk from 'chalk';
import prompts from 'prompts';
import { addProject } from '../config';

export async function addCommand(): Promise<void> {
  const answers = await prompts([
    {
      type: 'text',
      name: 'name',
      message: 'Project name (a short label):',
      validate: (v: string) => (v.trim().length > 0 ? true : 'Name is required'),
    },
    {
      type: 'text',
      name: 'url',
      message: 'Supabase project URL (https://xyz.supabase.co):',
      validate: (v: string) =>
        /^https?:\/\/.+\.supabase\.co\/?$/i.test(v.trim())
          ? true
          : 'Must look like https://<ref>.supabase.co',
    },
    {
      type: 'password',
      name: 'anonKey',
      message: 'Anon (public) API key:',
      validate: (v: string) => (v.trim().length > 20 ? true : 'Anon key looks too short'),
    },
    {
      type: 'text',
      name: 'table',
      message: 'Anon-readable table to read on each ping (blank = auth check only):',
      initial: 'keepalive',
    },
    {
      type: 'text',
      name: 'rpc',
      message: 'Heartbeat function that writes on each ping (blank = none):',
      initial: 'supawake_heartbeat',
    },
    {
      type: 'text',
      name: 'email',
      message: 'Email of a dedicated Auth user to sign in as (blank = stay anon):',
    },
    {
      type: (prev: string) => (prev && prev.trim() ? 'password' : null),
      name: 'password',
      message: 'Password for that user:',
    },
  ]);

  if (!answers.name || !answers.url || !answers.anonKey) {
    console.log(chalk.yellow('Cancelled.'));
    return;
  }

  const table = (answers.table ?? '').trim();
  const rpc = (answers.rpc ?? '').trim();
  const email = (answers.email ?? '').trim();
  const password = answers.password ?? '';

  try {
    addProject({
      name: answers.name.trim(),
      url: answers.url.trim().replace(/\/$/, ''),
      anonKey: answers.anonKey.trim(),
      ...(table ? { table } : {}),
      ...(rpc ? { rpc } : {}),
      ...(email && password ? { email, password } : {}),
    });
    console.log(chalk.green(`✓ Added project "${answers.name}"`));
    if (!table && !rpc) {
      console.log(
        chalk.yellow(
          '  No table set — pings will check auth only and will not keep the database awake.',
        ),
      );
    }
  } catch (err) {
    console.error(chalk.red(`✗ ${(err as Error).message}`));
    process.exitCode = 1;
  }
}
