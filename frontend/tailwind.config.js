// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: @type {import('tailwindcss').Config}
// Date: 2026-06-24
// ---------------------------------------------------------------------------
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#f3f2f1',
        'bg-sidebar': '#ffffff',
        surface: '#ffffff',
        'surface-hover': '#fafafa',
        'surface-raised': '#ffffff',
        hairline: '#e1dfdd',
        'hairline-strong': '#bfbdbb',
        ink: '#242424',
        'ink-dim': '#45413d',
        'ink-muted': '#605e5c',
        'ink-faint': '#797775',
        gold: {
          DEFAULT: '#0078d4',
          soft: '#106ebe',
          dim: '#005a9e',
        },
      },
      fontFamily: {
        display: ['"Segoe UI Variable Display"', '"Segoe UI"', 'system-ui', 'sans-serif'],
        sans: ['"Segoe UI Variable Text"', '"Segoe UI"', 'system-ui', 'sans-serif'],
        mono: ['"Cascadia Code"', '"Cascadia Mono"', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
}
