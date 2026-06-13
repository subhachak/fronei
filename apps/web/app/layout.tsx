import './globals.css'
import 'highlight.js/styles/github-dark.css'
import { ClerkProvider } from '@clerk/nextjs'

export const metadata = {
  title: 'Fronei',
  description: 'Your AI personal assistant',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark">
      <head>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.31.0/dist/tabler-icons.min.css" />
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content" />
        <link rel="icon" href="/fronei-logo.png" type="image/png" />
        <link rel="apple-touch-icon" href="/fronei-logo.png" />
        <link rel="manifest" href="/manifest.json" />
        <meta name="theme-color" content="#7c3aed" />
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-title" content="Fronei" />
        <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
      </head>
      <body>
        <ClerkProvider>
          {/* Reads localStorage before first paint to prevent theme flash */}
          <script
            dangerouslySetInnerHTML={{
              __html: `try{var t=localStorage.getItem('md-theme');if(t)document.documentElement.setAttribute('data-theme',t);var a=localStorage.getItem('md-accent');if(a&&a!=='default')document.documentElement.setAttribute('data-accent',a);}catch(e){}`,
            }}
          />
          {children}
        </ClerkProvider>
      </body>
    </html>
  )
}
