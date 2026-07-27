import type { MetadataRoute } from 'next'

const SITE_URL = 'https://www.fronei.com'

export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: '*', allow: '/', disallow: ['/app', '/admin'] },
    sitemap: `${SITE_URL}/sitemap.xml`,
  }
}
