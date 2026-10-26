/** Desktop entry point for the shared, role-aware populated-screen checks. */
process.env.READINESS_WIDTHS ??= '1440'
process.env.SWEEP_OUT ??= 'browser-sweep.json'
await import('./ui-readiness-sweep.mjs')
