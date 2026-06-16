/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: ['selector', '[data-theme="dark"]'],
  content: [
    './app/v2/**/*.{ts,tsx}',
    './app/admin/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
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
