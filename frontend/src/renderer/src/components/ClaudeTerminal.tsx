import { useCallback, useEffect, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { useWsEvent } from '@/services/ws'
import type { ClaudeEvent } from '@/types'

function asString(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined
}

function fmt(v: unknown): string {
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

const TERMINAL_THEME = {
  background: '#0f1115',
  foreground: '#e6e9ef',
  cursor: '#4a90e2',
  cursorAccent: '#0f1115',
  selectionBackground: 'rgba(74, 144, 226, 0.35)',
  black: '#171a21',
  red: '#ff5c57',
  green: '#5af78e',
  yellow: '#f3f99d',
  blue: '#57c7ff',
  magenta: '#ff6ac1',
  cyan: '#9aedfe',
  white: '#e6e9ef',
  brightBlack: '#8a93a6',
  brightRed: '#ff5c57',
  brightGreen: '#5af78e',
  brightYellow: '#f3f99d',
  brightBlue: '#57c7ff',
  brightMagenta: '#ff6ac1',
  brightCyan: '#9aedfe',
  brightWhite: '#ffffff',
}

export default function ClaudeTerminal(): JSX.Element {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const terminalRef = useRef<Terminal | null>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const term = new Terminal({
      convertEol: true,
      theme: TERMINAL_THEME,
      fontSize: 13,
      fontFamily: "'Cascadia Code', 'Fira Code', Consolas, 'Courier New', monospace",
    })
    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(container)
    fitAddon.fit()
    terminalRef.current = term

    const onResize = (): void => {
      fitAddon.fit()
    }
    window.addEventListener('resize', onResize)

    return () => {
      window.removeEventListener('resize', onResize)
      term.dispose()
      terminalRef.current = null
    }
  }, [])

  const onEvent = useCallback((raw: unknown): void => {
    const term = terminalRef.current
    if (!term) return
    const data = (raw ?? {}) as ClaudeEvent

    switch (data.type) {
      case 'assistant': {
        const text = asString(data.text)
        if (text) term.write(`${text}\r\n`)
        break
      }
      case 'result': {
        const text = asString(data.text) ?? ''
        const isError = data.is_error === true || String(data.subtype ?? '').includes('error')
        if (text) term.write(`${isError ? '\x1b[31m' : ''}${text}${isError ? '\x1b[0m' : ''}\r\n`)
        break
      }
      case 'tool_use': {
        const name = asString(data.name) ?? 'unknown'
        term.write(`\x1b[90m[tool] ${name}\x1b[0m\r\n`)
        break
      }
      case 'tool_result': {
        const value = data.result ?? data.content ?? data.text
        term.write(`\x1b[90m[result] ${fmt(value)}\x1b[0m\r\n`)
        break
      }
      case 'error': {
        const text = asString(data.message) ?? asString(data.text) ?? 'error'
        term.write(`\x1b[31m${text}\x1b[0m\r\n`)
        break
      }
      // system / init / raw — silent, not meaningful for the user
      case 'system':
      case 'init':
        break
      default:
        term.write(`${JSON.stringify(data)}\r\n`)
    }
  }, [])

  useWsEvent('claude_event', onEvent)

  return (
    <div
      ref={containerRef}
      style={{
        width: '100%',
        height: 380,
        background: '#0f1115',
        border: '1px solid #262b36',
        borderRadius: 8,
        padding: 8,
        overflow: 'hidden',
      }}
    />
  )
}
