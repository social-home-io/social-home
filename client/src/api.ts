import { token } from '@/store/auth'

class ApiClient {
  private base = ''

  private headers(): HeadersInit {
    return {
      'Content-Type': 'application/json',
      ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}),
    }
  }

  async get<T = any>(path: string, params?: Record<string, string>): Promise<T> {
    const url = params ? `${path}?${new URLSearchParams(params)}` : path
    const res = await fetch(url, { headers: this.headers() })
    if (res.status === 401) { import('@/store/auth').then(m => m.logout()); throw new Error('Unauthorized') }
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }

  async post<T = any>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: 'POST', headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (res.status === 401) { import('@/store/auth').then(m => m.logout()); throw new Error('Unauthorized') }
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }

  async put<T = any>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: 'PUT', headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }

  async patch<T = any>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: 'PATCH', headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }

  async delete(path: string): Promise<void> {
    await fetch(path, { method: 'DELETE', headers: this.headers() })
  }

  async upload<T = any>(path: string, body: FormData): Promise<T> {
    const headers: HeadersInit = token.value
      ? { Authorization: `Bearer ${token.value}` }
      : {}
    const res = await fetch(path, {
      method: 'POST',
      headers,
      body,
    })
    if (res.status === 401) { import('@/store/auth').then(m => m.logout()); throw new Error('Unauthorized') }
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }
}

export const api = new ApiClient()
