/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  async rewrites() {
    return {
      beforeFiles: [
        {
          source: '/',
          destination: '/agent-v3',
        },
      ],
    }
  },
}
module.exports = nextConfig
