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
      // On a needs_confirmation response the assistant "content" is just a placeholder
      // ("该操作需要确认"). Drop it so an approval re-sends the original request rather
      // than that placeholder text.
      set({
        messages: data.needs_confirmation ? history : [...history, assistant],
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
    try {
      await api.post(`/confirmations/${conf.confirmation_id}/approve`)
    } catch {
      // The confirmation expired or was already consumed — close the modal and
      // tell the user rather than leaving it stuck open.
      set({
        pendingConfirmation: null,
        messages: [...get().messages, { role: 'assistant', content: '确认已过期或无效，请重新发起该操作。' }],
      })
      return
    }
    set({ pendingConfirmation: null })

    // Re-send the original history (which now ends at the user request, since the
    // placeholder assistant message was dropped) with the confirmation id.
    const history = get().messages
    set({ sending: true })
    try {
      const { data } = await api.post('/ai/chat', {
        messages: history.map((m) => ({ role: m.role, content: m.content })),
        use_tools: true,
        confirmation_id: conf.confirmation_id,
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
    } catch {
      set({ sending: false })
    }
  },

  denyConfirmation: async () => {
    const conf = get().pendingConfirmation
    if (!conf) return
    // Close the modal immediately — even if the backend already expired the
    // confirmation (deny then 404s), the UI must not stay stuck open.
    set({ pendingConfirmation: null })
    try {
      await api.post(`/confirmations/${conf.confirmation_id}/deny`)
    } catch {
      // The backend may have already expired it; the modal is closed regardless.
    }
  },

  clear: () => set({ messages: [], pendingConfirmation: null, lastToolCalls: [] }),
}))
