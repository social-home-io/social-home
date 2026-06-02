/**
 * Host↔iframe postMessage bridge for sandboxed Social Home apps.
 *
 * Security model:
 * - Identity is verified by ``event.source === iframe.contentWindow``,
 *   NOT by ``event.origin``. Sandboxed iframes produce an opaque origin
 *   (``"null"``), so origin-checking is unreliable. Any event whose
 *   source is not our iframe is silently dropped.
 * - The bearer token MUST NEVER be included in any reply. The app
 *   receives only its own data and context (appId, selfUserId).
 * - Replies use target origin ``'*'`` because the iframe's opaque origin
 *   means a specific origin target would always miss.
 */

import { api, ApiError } from '@/api'
import { ws, WsEvent } from '@/ws'

export interface BridgeContext {
  appId: string
  selfUserId: string
}

/** RPC envelope sent by the app iframe. */
interface RpcRequest {
  id: string | number
  method: string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  params?: any // dynamic RPC payload — typed per method below
}

/** Success reply sent back to the iframe. */
interface RpcOkReply {
  id: string | number
  ok: true
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result: any
}

/** Failure reply sent back to the iframe. */
interface RpcErrReply {
  id: string | number
  ok: false
  error: { code: string; message: string }
}

type RpcReply = RpcOkReply | RpcErrReply

/**
 * Mount the bridge between ``iframe`` and the host.
 *
 * Returns a cleanup function that tears down both the ``message``
 * listener and the WS subscription — call it when the app frame
 * unmounts.
 */
export function mountBridge(
  iframe: HTMLIFrameElement,
  ctx: BridgeContext,
): () => void {
  const { appId } = ctx

  function reply(r: RpcReply): void {
    iframe.contentWindow?.postMessage(r, '*')
  }

  async function handleRpc(req: RpcRequest): Promise<void> {
    const { id, method, params } = req

    try {
      switch (method) {
        case 'store.get': {
          const key = String(params?.key ?? '')
          try {
            const resp = await api.get<{ key: string; value: unknown }>(
              `/api/apps/${encodeURIComponent(appId)}/store/${encodeURIComponent(key)}`,
            )
            reply({ id, ok: true, result: resp.value })
          } catch (err) {
            if (err instanceof ApiError && err.status === 404) {
              reply({ id, ok: true, result: null })
            } else {
              throw err
            }
          }
          break
        }

        case 'store.set': {
          const key = String(params?.key ?? '')
          const value = params?.value
          await api.put(`/api/apps/${encodeURIComponent(appId)}/store/${encodeURIComponent(key)}`, { value })
          reply({ id, ok: true, result: { ok: true } })
          break
        }

        case 'store.delete': {
          const key = String(params?.key ?? '')
          await api.delete(`/api/apps/${encodeURIComponent(appId)}/store/${encodeURIComponent(key)}`)
          reply({ id, ok: true, result: { ok: true } })
          break
        }

        case 'store.list': {
          const resp = await api.get<{ items: unknown[] }>(`/api/apps/${encodeURIComponent(appId)}/store`)
          reply({ id, ok: true, result: resp.items })
          break
        }

        case 'app.context': {
          // NEVER include a token — the app runs in a sandboxed iframe
          // and must not gain host credentials.
          reply({ id, ok: true, result: { appId: ctx.appId, selfUserId: ctx.selfUserId } })
          break
        }

        case 'app.send': {
          const { session_id, peer_instance_id, payload } = params ?? {}
          await api.post(`/api/apps/${encodeURIComponent(appId)}/messages`, {
            session_id,
            peer_instance_id,
            payload,
          })
          reply({ id, ok: true, result: { ok: true } })
          break
        }

        case 'peers.list': {
          const resp = await api.get<{ peers: unknown[] }>(
            `/api/apps/${encodeURIComponent(appId)}/peers`,
          )
          reply({ id, ok: true, result: resp.peers })
          break
        }

        case 'app.openSession': {
          const { peer_instance_id } = params ?? {}
          const resp = await api.post<{ session_id: string }>(
            `/api/apps/${encodeURIComponent(appId)}/sessions`,
            { peer_instance_id },
          )
          reply({ id, ok: true, result: resp.session_id })
          break
        }

        default:
          reply({
            id,
            ok: false,
            error: { code: 'unknown_method', message: `Unknown method: ${method}` },
          })
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      reply({ id, ok: false, error: { code: 'error', message } })
    }
  }

  function onMessage(event: MessageEvent): void {
    // Security: reject any message not originating from our iframe.
    // Do NOT check event.origin — sandboxed iframes emit "null".
    if (event.source !== iframe.contentWindow) return

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const req = event.data as any
    if (!req || typeof req.method !== 'string') return

    // eslint-disable-next-line @typescript-eslint/no-floating-promises
    handleRpc(req as RpcRequest)
  }

  // WS relay: push app-specific real-time frames into the iframe.
  const offWs = ws.on('app.message', (evt: WsEvent) => {
    if (evt.data?.app_id === appId) {
      iframe.contentWindow?.postMessage(
        { type: 'app:event', payload: evt.data.payload },
        '*',
      )
    }
  })

  window.addEventListener('message', onMessage)

  return () => {
    window.removeEventListener('message', onMessage)
    offWs()
  }
}
