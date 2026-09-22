export const LS_WS_URL = 'hud-router-ws-url'
export const BACKOFF_CAP_MS = 10_000
export const FALLBACK_HOST = '192.168.3.2'

export type ConnState = 'disconnected' | 'connecting' | 'connected' | 'invalid url' | string

export function defaultWsUrl(): string {
  let host = ''
  try {
    host = location?.hostname ? String(location.hostname).trim() : ''
  } catch {
    host = ''
  }
  if (!host) host = FALLBACK_HOST
  return `ws://${host}:8766`
}

export function loadSavedUrl(): string {
  const fallback = defaultWsUrl()
  try {
    const saved = (localStorage.getItem(LS_WS_URL) || '').trim()
    return saved || fallback
  } catch {
    return fallback
  }
}

export function persistUrl(url: string): void {
  try {
    localStorage.setItem(LS_WS_URL, url.trim())
  } catch {
    /* ignore */
  }
}

export function validateWsUrl(raw: string): string {
  const url = (raw || '').trim()
  if (!url) {
    throw new Error(
      'WebSocket URL 为空（灰色占位符不是真实值；请点输入框确认，或使用默认 ws://<页面hostname>:8766）',
    )
  }
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    throw new Error('无法解析 WebSocket URL: ' + url)
  }
  if (parsed.protocol !== 'ws:' && parsed.protocol !== 'wss:') {
    throw new Error('只支持 ws:// 与 wss://，收到: ' + parsed.protocol)
  }
  return url
}

export type WsHandlers = {
  onState: (state: ConnState) => void
  onMessage: (raw: string) => void
  onError: (err: unknown) => void
}

export function createWsClient(handlers: WsHandlers) {
  let socket: WebSocket | null = null
  let wantConnected = false
  let reconnectTimer: number | null = null
  let backoffMs = 500

  function clearReconnect() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  function scheduleReconnect(open: (url: string) => void, url: string) {
    if (!wantConnected) return
    clearReconnect()
    const wait = backoffMs
    backoffMs = Math.min(backoffMs * 2, BACKOFF_CAP_MS)
    handlers.onState(`reconnect in ${wait}ms`)
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      open(url)
    }, wait)
  }

  function open(url: string) {
    handlers.onState('connecting')
    let ws: WebSocket
    try {
      ws = new WebSocket(url)
    } catch (err) {
      handlers.onError(err)
      handlers.onState('error')
      scheduleReconnect(open, url)
      return
    }
    socket = ws
    ws.onopen = () => {
      if (socket !== ws) return
      backoffMs = 500
      handlers.onState('connected')
    }
    ws.onmessage = ev => {
      if (socket !== ws) return
      handlers.onMessage(String(ev.data))
    }
    ws.onerror = ev => {
      if (socket !== ws) return
      const hint = (ev as ErrorEvent).message || 'WebSocket error（详情见 onclose）'
      handlers.onError(new Error(hint))
    }
    ws.onclose = ev => {
      if (socket !== ws) return
      socket = null
      const detail = `WebSocket closed code=${ev.code} reason=${ev.reason || ''} wasClean=${ev.wasClean}`
      if (wantConnected) {
        handlers.onError(new Error(detail))
        scheduleReconnect(open, url)
      } else {
        handlers.onState('disconnected')
      }
    }
  }

  return {
    connect(rawUrl: string) {
      let url = (rawUrl || '').trim()
      if (!url) url = defaultWsUrl()
      url = validateWsUrl(url)
      persistUrl(url)
      wantConnected = true
      backoffMs = 500
      clearReconnect()
      if (socket) {
        try {
          socket.close()
        } catch (err) {
          handlers.onError(err)
        }
        socket = null
      }
      open(url)
    },
    disconnect() {
      wantConnected = false
      clearReconnect()
      if (socket) {
        try {
          socket.close()
        } catch (err) {
          handlers.onError(err)
        }
        socket = null
      }
      handlers.onState('disconnected')
    },
    get wantConnected() {
      return wantConnected
    },
  }
}
