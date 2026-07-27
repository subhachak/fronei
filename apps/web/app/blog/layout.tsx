import { IBM_Plex_Mono, IBM_Plex_Sans, IBM_Plex_Serif } from 'next/font/google'
import { CtaFooter } from '../components/marketing/CtaFooter'
import { MarketingNav } from '../components/marketing/MarketingNav'

const plexSerif = IBM_Plex_Serif({
  subsets: ['latin'],
  weight: ['500', '600', '700'],
  variable: '--font-marketing-serif',
})
const plexSans = IBM_Plex_Sans({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-marketing-sans',
})
const plexMono = IBM_Plex_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-marketing-mono',
})

export default function BlogLayout({ children }: { children: React.ReactNode }) {
  return (
    <div
      className={`${plexSerif.variable} ${plexSans.variable} ${plexMono.variable} h-full w-full overflow-y-auto bg-stone-50 font-[family-name:var(--font-marketing-sans)] text-stone-900`}
    >
      <MarketingNav />
      {children}
      <CtaFooter />
    </div>
  )
}
