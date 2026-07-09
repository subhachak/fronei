import type { Metadata } from 'next'
import { IBM_Plex_Mono, IBM_Plex_Sans, IBM_Plex_Serif } from 'next/font/google'
import { MarketingNav } from './components/marketing/MarketingNav'
import { Hero } from './components/marketing/Hero'
import { CitationDemo } from './components/marketing/CitationDemo'
import { WhatItCatches } from './components/marketing/WhatItCatches'
import { DomainsGrid } from './components/marketing/DomainsGrid'
import { HowItWorks } from './components/marketing/HowItWorks'
import { FlagshipWorkflow } from './components/marketing/FlagshipWorkflow'
import { SecurityGovernance } from './components/marketing/SecurityGovernance'
import { CtaFooter } from './components/marketing/CtaFooter'

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

const TITLE = 'Fronei — The evidentiary research engine for regulated decisions'
const DESCRIPTION =
  'Fronei traces every claim to its source, flags what’s gone stale, and surfaces conflicting evidence — built for vendor and tech risk due diligence, and the diligence work regulated teams have to defend.'

export const metadata: Metadata = {
  title: TITLE,
  description: DESCRIPTION,
  openGraph: {
    title: TITLE,
    description: DESCRIPTION,
    type: 'website',
  },
}

export default function MarketingHomePage() {
  return (
    <main
      className={`${plexSerif.variable} ${plexSans.variable} ${plexMono.variable} h-full w-full overflow-y-auto bg-stone-50 font-[family-name:var(--font-marketing-sans)] text-stone-900`}
    >
      <MarketingNav />
      <Hero />
      <div className="mx-auto w-full max-w-6xl px-6 pb-20">
        <CitationDemo />
      </div>
      <WhatItCatches />
      <DomainsGrid />
      <HowItWorks />
      <FlagshipWorkflow />
      <SecurityGovernance />
      <CtaFooter />
    </main>
  )
}
