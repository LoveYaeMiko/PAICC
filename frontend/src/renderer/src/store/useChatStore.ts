import { create } from 'zustand'
import { api } from '@/services/api'
import type { ChatMessage, Confirmation, ToolCall } from '@/types'

interface ChatState {
  messages: ChatMessage[]
  sending: boolean
  pendingConfirmation: Confirmation | null
  lastToolCalls: ToolCall[]
  send: (text: string, confirmationId?: string) => Promise<void>
  approveConfirmation: () => Promise<void>
  denyConfirmation: () => Promise<void>
  clear: () => void
}

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  sending: false,
  pendingConfirmation: null,
  lastToolCalls: [],

  send: async (text: string, confirmationId?: string) => {
    const user: ChatMessage = { role: 'user', content: text }
    const history = [...get().messages, user]
    set({ messages: history, sending: true, pendingConfirmation: null })
    try {
      const { data } = await api.post('/ai/chat', {
        messages: history.map((m) => ({ role: m.role, content: m.content })),
        use_tools: true,
        confirmation_id: confirmationId,
      })
      const assistant: ChatMessage = {
        role: 'assistant',
        content: data.content,
        tool_calls: data.tool_calls,
      }
      set({
        messages: [...history, assistant],
        sending: false,
        pendingConfirmation: data.needs_confirmation ? data.confirmation : null,
        lastToolCalls: data.tool_calls ?? [],
      })
    } catch (err) {
      set({
        sending: false,
        messages: [...history, { role: 'assistant', content: '请求失败：请检查后端连接。' }],
      })
    }
  },

  approveConfirmation: async () => {
    const conf = get().pendingConfirmation
    if (!conf) return
    await api.post(`/confirmations/${conf.confirmation_id}/approve`)
    set({ pendingConfirmation: null })
    const last = get().messages[get().messages.length - 1]
    await get().send(last?.content ?? '', conf.confirmation_id)
  },

  denyConfirmation: async () => {
    const conf = get().pendingConfirmation
    if (!conf) return
    await api.post(`/confirmations/${conf.confirmation_id}/deny`)
    set({ pendingConfirmation: null })
  },

  clear: () => set({ messages: [], pendingConfirmation: null, lastToolCalls: [] }),
}))
