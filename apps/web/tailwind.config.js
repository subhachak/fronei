/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: ['selector', '[data-theme="dark"]'],
  content: [
    './app/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      // Remap neutral scale to brand navy (slate-tinted) so dark:bg-neutral-950
      // etc. render as brand navy rather than pure zinc/gray.
      colors: {
        neutral: {
          50:  '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
          800: '#1e293b',
          900: '#0f172a',
          950: '#0a0f1e',
        },
        // Brand gold for explicit accent usage
        gold: {
          DEFAULT: '#fbbf24',
          light:   '#fcd34d',
          dark:    '#d97706',
        },
        background: 'var(--bg-base)',
        foreground: 'var(--t1)',
        muted: 'var(--bg-s1)',
        'muted-foreground': 'var(--t3)',
        border: 'var(--bd)',
        input: 'var(--bg-input)',
        ring: 'var(--ac)',
        primary: {
          DEFAULT: 'var(--ac)',
          foreground: 'var(--ac-text)',
        },
        secondary: {
          DEFAULT: 'var(--bg-s2)',
          foreground: 'var(--t2)',
        },
        accent: {
          DEFAULT: 'var(--ac-bg)',
          foreground: 'var(--ac-text)',
        },
        destructive: {
          DEFAULT: 'var(--destructive)',
          foreground: 'var(--destructive-foreground)',
        },
        card: {
          DEFAULT: 'var(--bg-s1)',
          foreground: 'var(--t1)',
        },
        sidebar: 'var(--bg-nav)',
      },
      fontFamily: {
        sans: ['var(--font-sans)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
