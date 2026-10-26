/** Responsive entry point; details, tabs, forms and portal sessions are shared. */
process.env.READINESS_WIDTHS ??= '375,768,1440'
process.env.SWEEP_OUT ??= 'responsive-sweep.json'
await import('./ui-readiness-sweep.mjs')
