/**
 * Settings of this cluster, read from `site.json` next to index.html when the page loads, so one build serves
 * any cluster. The deploy writes the file from cluster.yml. This is the contract with the deploy side; keep it
 * exactly:
 *
 *   {
 *     "cluster_name": str,          // shown as the brand and page title, and used as the SSH host alias
 *     "logo": str | null,           // relative URL of the logo next to index.html, such as "logo.png";
 *                                   // null shows a small generic mark
 *     "login_address": str,         // host name users SSH to
 *     "home_quota_soft_gb": int,    // per-user /home quotas
 *     "home_quota_hard_gb": int,
 *     "scratch_cleanup_days": int   // staged scratch data unused this long is deleted
 *   }
 *
 * Monitor mode instead uses {"mode":"monitor", "cluster_name", "logo", "login_address", "users"}.
 * Partitions, policies, and limits are not here: they come from data/status.json in Slurm mode.
 */
export type SiteSettings = {
  mode?: never
  cluster_name: string
  logo: string | null
  login_address: string
  home_quota_soft_gb: number
  home_quota_hard_gb: number
  scratch_cleanup_days: number
}

export type MonitorSiteSettings = {
  mode: 'monitor'
  cluster_name: string
  logo: string | null
  login_address: string
  users: string[]
}

export type LoadedSiteSettings = SiteSettings | MonitorSiteSettings

const settingNames = ['cluster_name', 'logo', 'login_address', 'home_quota_soft_gb', 'home_quota_hard_gb', 'scratch_cleanup_days']

/** Check site.json's content against the contract above and name the first problem. */
export function checkSiteSettings(value: unknown): LoadedSiteSettings {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('site.json must hold an object')
  const settings = value as Record<string, unknown>
  if (settings.mode === 'monitor') {
    const allowed = ['mode', 'cluster_name', 'logo', 'login_address', 'users']
    const unknown = Object.keys(settings).filter(name => !allowed.includes(name))
    const missing = allowed.filter(name => !(name in settings))
    if (unknown.length || missing.length) throw new Error(`site.json: unknown settings [${unknown.join(', ')}], missing settings [${missing.join(', ')}]`)
    for (const name of ['cluster_name', 'login_address']) {
      if (typeof settings[name] !== 'string' || !settings[name]) throw new Error(`site.json: ${name} must be a non-empty string`)
    }
    if (!Array.isArray(settings.users) || !settings.users.every(user => typeof user === 'string' && user.length > 0)) {
      throw new Error('site.json: users must be a list of login names')
    }
    if (settings.logo !== null && (typeof settings.logo !== 'string' || !settings.logo || settings.logo.startsWith('/') || settings.logo.includes(':'))) {
      throw new Error('site.json: logo must be null or a relative URL such as "logo.png"')
    }
    return settings as MonitorSiteSettings
  }
  const names = Object.keys(settings)
  const unknown = names.filter(name => !settingNames.includes(name))
  const missing = settingNames.filter(name => !names.includes(name))
  if (unknown.length || missing.length) throw new Error(`site.json: unknown settings [${unknown.join(', ')}], missing settings [${missing.join(', ')}]`)
  for (const name of ['cluster_name', 'login_address']) {
    if (typeof settings[name] !== 'string' || !settings[name]) throw new Error(`site.json: ${name} must be a non-empty string`)
  }
  for (const name of ['home_quota_soft_gb', 'home_quota_hard_gb', 'scratch_cleanup_days']) {
    if (!Number.isInteger(settings[name])) throw new Error(`site.json: ${name} must be a whole number`)
  }
  const logo = settings.logo
  // A relative URL keeps the logo under the site's own path.
  if (logo !== null && (typeof logo !== 'string' || !logo || logo.startsWith('/') || logo.includes(':'))) {
    throw new Error('site.json: logo must be null or a relative URL such as "logo.png"')
  }
  return settings as SiteSettings
}

/** Load and check site.json from the page's own folder. */
export async function loadSiteSettings(): Promise<LoadedSiteSettings> {
  const response = await fetch('site.json', { cache: 'no-store' })
  if (!response.ok) throw new Error(`site.json could not be loaded (HTTP ${response.status})`)
  return checkSiteSettings(await response.json())
}
