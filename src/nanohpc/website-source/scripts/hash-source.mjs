// Write ../website/build-source.sha256 (the build output next to this source folder): one SHA-256 over the
// website source, so a test can tell whether the committed build is older than the source.
// tests/test_website_build.py (at the repo root) computes the same hash; keep both in step.
import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const website = fileURLToPath(new URL('..', import.meta.url))
const output = join(website, '..', 'website', 'build-source.sha256')
const files = ['index.html', 'package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts']
const folders = ['src', 'public']
const ignored = new Set(['.DS_Store'])

/** List every file under a folder, as paths relative to this source folder, with forward slashes. */
function listFiles(folder) {
  return readdirSync(join(website, folder), { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && !ignored.has(entry.name))
    .map((entry) => relative(website, join(entry.parentPath, entry.name)).split(sep).join('/'))
}

const paths = [...files, ...folders.flatMap(listFiles)].sort()
const hash = createHash('sha256')
for (const path of paths) {
  const content = readFileSync(join(website, path))
  hash.update(`${path}\0${content.length}\0`)
  hash.update(content)
}
writeFileSync(output, `${hash.digest('hex')}\n`)
console.log(`Wrote ${relative(website, output)} over ${paths.length} source files`)
