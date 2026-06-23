export const BRAND_ASSET_VERSION = '20260623-42e2015'

export function brandAsset(path: string) {
  return `${path}?v=${BRAND_ASSET_VERSION}`
}
