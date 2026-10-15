import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

/**
 * Vendor chunk assignment, by package rather than by dependency closure.
 *
 * This is deliberately the function form. The `{ name: [packages] }` object form
 * assigns each listed package *and everything it transitively depends on*, and
 * whichever group's resolution finishes first claims a shared dependency. That
 * is not deterministic, and here it produced two real defects:
 *
 *  - `react` was claimed by `query-vendor`, because it is a dependency of
 *    @tanstack/react-query, while `react-dom` stayed in `react-vendor`. Since
 *    @tanstack/react-virtual imports `flushSync` from react-dom, the two chunks
 *    ended up importing each other and Rollup warned about a circular chunk.
 *    Chunk cycles are a real hazard, not cosmetic: execution order between them
 *    is what decides whether a binding is initialised when it is first read.
 *
 *  - `clsx` was swept into `chart-vendor`, because recharts depends on it. But
 *    `cn()` in lib/utils uses clsx on essentially every component, so the eager
 *    app chunk statically imported the 393 kB recharts chunk, and index.html
 *    module-preloaded it. Every user downloaded the charting library on first
 *    paint - precisely what splitting it out was supposed to prevent.
 *
 * Matching on the package directory assigns exactly the named libraries and
 * nothing else, and leaves genuinely shared utilities to be placed by Rollup in
 * a chunk every consumer can reach.
 */
const VENDOR_CHUNKS: [RegExp, string][] = [
  // Kept together on purpose: react-dom and react-router read react's internals,
  // so separating them is what creates a cycle.
  [
    /^(react|react-dom|scheduler|react-router|react-router-dom|@remix-run\/router)$/,
    'react-vendor',
  ],
  [/^@tanstack\//, 'query-vendor'],
  // The class-name utilities behind `cn()`. Named explicitly because they are
  // tiny and used by every component: left unnamed, Rollup folds a chunk this
  // small into whichever chunk it happens to sit next to, and when that was the
  // charting chunk the eager app chunk had to import it to render a button.
  [/^(clsx|tailwind-merge|class-variance-authority)$/, 'ui-vendor'],
  [/^(recharts|recharts-scale|react-smooth|victory-vendor|internmap|d3-[a-z-]+)$/, 'chart-vendor'],
  [/^(react-hook-form|zod|@hookform\/resolvers)$/, 'form-vendor'],
]

function vendorChunk(id: string): string | undefined {
  // Rollup ids are absolute; on Windows they arrive with backslashes.
  const normalised = id.replace(/\\/g, '/')
  const marker = '/node_modules/'
  const at = normalised.lastIndexOf(marker)
  if (at === -1) return undefined

  const segments = normalised.slice(at + marker.length).split('/')
  const first = segments[0] ?? ''
  const pkg = first.startsWith('@') ? `${first}/${segments[1] ?? ''}` : first

  for (const [pattern, chunk] of VENDOR_CHUNKS) {
    if (pattern.test(pkg)) return chunk
  }
  return undefined
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, '../..'), 'VITE_')

  return {
    plugins: [tailwindcss(), react()],

    resolve: {
      alias: { '@': path.resolve(__dirname, './src') },
    },

    server: {
      host: true, // required so the container port mapping reaches the dev server
      port: 5173,
      strictPort: true,
      // Hybrid development (master rules 49A mode 1): the API runs on the host
      // while Postgres/Redis run in Docker. Proxying keeps the browser on one
      // origin, so no CORS preflight in the inner development loop.
      proxy: {
        '/api': {
          target: env.VITE_API_PROXY_TARGET || 'http://localhost:8000',
          changeOrigin: true,
        },
      },
      watch: {
        // Bind mounts on Windows/macOS do not deliver inotify events reliably.
        usePolling: process.env.VITE_USE_POLLING === 'true',
      },
    },

    // `vite preview` serves the production bundle, which is what the end-to-end
    // and browser checks drive - the dev server recompiles on every edit and so
    // cannot give a stable target. It does not inherit `server.proxy`, so the
    // same rule is restated here; without it every request from the built app
    // 404s on the static server and the screens look empty rather than broken.
    preview: {
      port: 4173,
      strictPort: true,
      proxy: {
        '/api': {
          target: env.VITE_API_PROXY_TARGET || 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },

    build: {
      outDir: 'dist',
      sourcemap: mode !== 'production',
      target: 'es2022',
      chunkSizeWarningLimit: 900,
      rollupOptions: {
        output: {
          // Split the heavy, rarely-changing libraries out of the app chunk so a
          // deploy does not invalidate the whole bundle in every user's cache.
          manualChunks: vendorChunk,
        },
      },
    },

    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
      css: false,
    },
  }
})
