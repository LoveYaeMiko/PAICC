import { useEffect } from 'react'
import { WS_URL } from './api'

type WsHandler = (data: unknown) => void

class WSClient {
  private socket: WebSocket | null = null
  private handlers = new Map<string, Set<WsHandler>>()
  private reconnectTimer: number | null = null

  connect(): void {
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return
    }
    try {
      this.socket = new WebSocket(WS_URL)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.socket.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data)
        if (msg && typeof msg.event === 'string') {
          this.handlers.get(msg.event)?.forEach((cb) => cb(msg.data))
        }
      } catch {
        /* ignore malformed frames */
      }
    }
    this.socket.onclose = () => this.scheduleReconnect()
    this.socket.onerror = () => this.socket?.close()
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer != null) return
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, 3000)
  }

  on(event: string, cb: WsHandler): () => void {
    if (!this.handlers.has(event)) this.handlers.set(event, new Set())
    this.handlers.get(event)!.add(cb)
    return () => this.off(event, cb)
  }

  off(event: string, cb: WsHandler): void {
    this.handlers.get(event)?.delete(cb)
  }
}

export const ws = new WSClient()

export function useWsEvent(event: string, handler: WsHandler): void {
  useEffect(() => {
    ws.connect()
    return ws.on(event, handler)
  }, [event, handler])
}
