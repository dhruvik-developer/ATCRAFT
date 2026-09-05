/**
 * The one place the end-to-end configuration is read.
 *
 * Every consumer - the Playwright config, the specs, the sweep scripts - takes
 * its URLs and credentials from here, so a default cannot differ between two
 * files that run in the same session. Before this existed the config pointed
 * `page.goto` at :5173 while a spec in the same run built absolute URLs to
 * :4173, and `.env.example` described variables that nothing loaded.
 *
 * `tests/e2e/.env` is loaded on first import when it exists. Values already in
 * the environment win, so an operator's `E2E_BASE_URL=... npx playwright test`
 * still overrides the file. Node's own loader is used rather than a dependency.
 */

import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const ENV_FILE = fileURLToPath(new URL('../.env', import.meta.url))
if (existsSync(ENV_FILE)) process.loadEnvFile(ENV_FILE)

/** Where the SPA is served. `make dev` runs Vite on :5173. */
export const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:5173'

/** The API, including its version prefix. */
export const API_URL = process.env.E2E_API_URL ?? 'http://localhost:8000/api/v1'

/**
 * The administrator the suite drives. Must match `src.core.seed.DEFAULT_ADMIN_EMAIL`,
 * and must not be a `.local` address: Pydantic's `EmailStr` refuses special-use
 * TLDs at validation, before the password is ever checked.
 */
export const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? 'admin@atcraft.example'

/** The password the suite signs in with once the account is bootstrapped. */
export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'ChangeMe!Atcraft2026'

/**
 * The password the seed was run with (`--admin-password`). The seed always
 * flags the account `must_change_password`, and the API refuses every business
 * route until it is replaced, so on a fresh database the suite signs in with
 * this, replaces it with `ADMIN_PASSWORD`, and never needs it again.
 */
export const BOOTSTRAP_PASSWORD = process.env.E2E_BOOTSTRAP_PASSWORD ?? ''
