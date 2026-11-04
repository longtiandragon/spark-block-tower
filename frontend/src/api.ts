export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}

export async function request<T>(path: string, options: { body?: unknown; token?: string; method?: string; timeout?: number } = {}): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), options.timeout ?? 4500)
  try {
    const headers: Record<string, string> = {}
    if (options.body !== undefined) headers['Content-Type'] = 'application/json'
    if (options.token) headers['X-Control-Token'] = options.token
    const response = await fetch(path, {
      method: options.method ?? (options.body !== undefined ? 'POST' : 'GET'),
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: controller.signal,
      cache: 'no-store',
    })
    if (!response.ok) {
      const detail = await response.json().catch(() => null)
      const message = typeof detail?.detail === 'string' ? detail.detail
        : response.status === 401 || response.status === 403 ? '控制令牌不正确，请在设置中填写。'
          : response.status === 409 ? '当前阶段无法执行此操作，请刷新状态后重试。'
            : `请求失败（${response.status}）`
      throw new ApiError(message, response.status)
    }
    if (response.status === 204) return undefined as T
    return await response.json() as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (error instanceof DOMException && error.name === 'AbortError') throw new Error('请求超时，请检查后台是否运行。')
    throw new Error('无法连接后台，请检查服务与网络。')
  } finally {
    window.clearTimeout(timeout)
  }
}
