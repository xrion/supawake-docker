function loadConfigFromEnvironment(): Config | null {
  const projects: Project[] = [];

  for (let i = 1; ; i++) {
    const url = process.env[`SUPABASE_${i}_URL`];
    const anonKey = process.env[`SUPABASE_${i}_KEY`];

    if (!url && !anonKey) {
      // On continue à chercher les éventuels indices suivants.
      // Cela permet d'avoir par exemple 1, 2 et 5.
      const hasLaterProject = Object.keys(process.env).some(
        (key) =>
          /^SUPABASE_\d+_(URL|KEY|NAME)$/.test(key) &&
          Number(key.split('_')[1]) > i
      );

      if (!hasLaterProject) {
        break;
      }

      continue;
    }

    if (!url || !anonKey) {
      throw new Error(
        `Incomplete configuration for SUPABASE_${i}. ` +
        `Both SUPABASE_${i}_URL and SUPABASE_${i}_KEY are required.`
      );
    }

    const name =
      process.env[`SUPABASE_${i}_NAME`] ||
      `supabase-${i}`;

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
        process.env.SUPAWAKE_INTERVAL || '0 3 */2 * *',
      notifications: {
        enabled: false,
        webhookUrl: '',
      },
    },
  };
}
