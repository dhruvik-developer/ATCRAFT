/**
 * Fail if src/types/openapi.ts is not what the current contract generates.
 *
 * The frontend's API types are generated from ../../openapi.json, which is
 * itself generated from the FastAPI app. This script is the guard that keeps
 * the three in step: it regenerates into a temporary file and compares.
 *
 * Why a guard is needed at all: these types used to be hand-written, so `tsc`
 * passed while screens were broken - a hand-written type cannot disagree with
 * the compiler, only with the server. The ageing report changed shape, the
 * ageing screen kept compiling, and it stopped working. A generated type turns
 * that into a build failure.
 *
 * Run `npm run contract:types` to fix a failure here. If the CONTRACT itself is
 * stale (the API changed but openapi.json did not), the backend guard catches
 * that first: `make contract-check`.
 */

import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = resolve(fileURLToPath(import.meta.url), '..')
const web = resolve(here, '..')
const spec = resolve(web, '..', '..', 'openapi.json')
const committed = join(web, 'src', 'types', 'openapi.ts')

const scratch = mkdtempSync(join(tmpdir(), 'atcraft-contract-'))
const generated = join(scratch, 'api.ts')

try {
  // Run the generator's entry point through this same node binary rather than
  // through `npx`. On Windows `npx` is `npx.cmd`, and current Node refuses to
  // spawnSync a `.cmd` without a shell (EINVAL); enabling a shell to get round
  // that would then put a path containing spaces through quoting rules that
  // differ per platform.
  const cli = join(web, 'node_modules', 'openapi-typescript', 'bin', 'cli.js')
  try {
    execFileSync(process.execPath, [cli, spec, '-o', generated], {
      cwd: web,
      stdio: 'pipe',
    })
  } catch (err) {
    console.error(
      'Could not run openapi-typescript.\n' +
        (err.stderr?.toString() || err.stdout?.toString() || err.message) +
        '\nIs it installed?  npm install',
    )
    process.exit(1)
  }

  const fresh = readFileSync(generated, 'utf8')

  let current
  try {
    current = readFileSync(committed, 'utf8')
  } catch {
    console.error(
      `${committed} does not exist.\nRun:  npm run contract:types`,
    )
    process.exit(1)
  }

  if (fresh !== current) {
    console.error(
      'src/types/openapi.ts does not match the OpenAPI contract.\n' +
        'The API changed and the generated types were not regenerated, so the\n' +
        'frontend is describing a server that no longer exists.\n\n' +
        'Run:  npm run contract:types',
    )
    process.exit(1)
  }

  console.log('src/types/openapi.ts matches the OpenAPI contract.')
} finally {
  rmSync(scratch, { recursive: true, force: true })
}
