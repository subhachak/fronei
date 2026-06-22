const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000'

export type TokenGetter = () => Promise<string | null>

export function createApiClient(getToken: TokenGetter) {
  async function authorizedFetch(path: string, init: RequestInit = {}): Promise<Response> {
    const token = await getToken()
    const isFormData = typeof FormData !== 'undefined' && init.body instanceof FormData
    const headers = {
      ...(!isFormData && init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    }
    return fetch(`${API_BASE}${path}`, { ...init, headers })
  }
  return { authorizedFetch }
}

export async function readErrorBody(response: Response, fallback: string): Promise<string> {
  try {
    const text = await response.text()
    return text || fallback
  } catch {
    return fallback
  }
}
