import { token } from '@/store/auth'
import { wsUrl } from '@/baseUrl'

export interface WsEvent {
  type: string
  data: Record<string, unknown>
}

type WsHandler = (event: WsEvent) => void

class WsManager {
  private ws: WebSocket | null = null
  private handlers = new Map<string, Set<WsHandler>>()
  private retryDelay = 5000

  connect() {
    // Anchor on ``document.baseURI`` so the URL is correct under HA
    // Supervisor ingress (where the document base is
    // ``/api/hassio_ingress/<token>/`` and the WebSocket needs to
    // reach the add-on through the same ingress prefix).
    const base = wsUrl('api/ws')
    const url = token.value
      ? `${base}?token=${encodeURIComponent(token.value)}`
      : base
    this.ws = new WebSocket(url)

    this.ws.onopen = () => { this.retryDelay = 5000 }

    this.ws.onmessage = (e) => {
      // Server broadcasts arrive as a flat object ``{type, ...body}``
      // (see ``RealtimeService._broadcast_*``). Handlers read
      // ``evt.data.x`` so we repackage the parsed frame into
      // ``{type, data: raw}`` here — keeping the whole payload
      // accessible under ``data`` while still giving handlers a
      // stable ``type`` field on the outer envelope.
      const raw = JSON.parse(e.data) as Record<string, unknown>
      const type = String(raw.type ?? '')
      const evt: WsEvent = { type, data: raw }
      this.handlers.get(type)?.forEach(h => h(evt))
      this.handlers.get('*')?.forEach(h => h(evt))
    }

    this.ws.onclose = () => {
      setTimeout(() => this.connect(), this.retryDelay)
      this.retryDelay = Math.min(this.retryDelay * 2, 60_000)
    }
  }

  on(type: string, handler: WsHandler) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set())
    this.handlers.get(type)!.add(handler)
    return () => { this.handlers.get(type)?.delete(handler) }
  }

  send(type: string, data: Record<string, unknown> = {}) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type, data }))
    }
  }

  disconnect() {
    this.ws?.close()
    this.ws = null
  }
}

export const ws = new WsManager()
