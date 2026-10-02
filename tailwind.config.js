/**
 * Tailwind v3.4.19 configuration for the compiled stylesheet (spec §6.1, D4).
 * Built by scripts/build_css.sh into static/css/app.css; ci.sh fails when the
 * committed file differs from a fresh build.
 *
 * `safelist` holds every utility class the code assembles at runtime, which a
 * content scan cannot see (inventory R1-R5, Task 1A-1 of
 * docs/plans/2026-10-01-web-ui-remediation-phase-1.md). tests/unit/test_compiled_css.py
 * derives the same set from its sources and fails on a new assembling site.
 */
module.exports = {
  content: ["./templates/**/*.html", "./static/js/**/*.js", "./src/**/*.py"],
  safelist: [
    // R1, R2: discussions status cards (templates/{admin,manager}/discussions.html);
    // their count text is text-<colour>-700, listed under R3
    "hover:border-gray-300", "hover:border-blue-300", "hover:border-green-300",
    "hover:border-amber-300", "hover:border-red-300",
    "ring-gray-400", "ring-blue-400", "ring-green-400", "ring-amber-400", "ring-red-400",
    // R3: thread status chips (templates/admin/_discussions_threads.html)
    "bg-gray-100", "bg-blue-100", "bg-green-100", "bg-amber-100", "bg-red-100",
    "text-gray-700", "text-blue-700", "text-green-700", "text-amber-700", "text-red-700",
    // R4: job status counts (templates/admin/jobs.html); the other colours are under R3
    "text-yellow-700",
    // R5: band_class (src/services/bands.py) muted shade; the strong shades are under R3
    "text-gray-600",
  ],
  theme: { extend: {} },
  plugins: [],
};
