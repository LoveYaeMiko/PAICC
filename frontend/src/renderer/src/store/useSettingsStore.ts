import { create } from 'zustand'
import { api } from '@/services/api'

interface SettingsState {
  settings: Record<string, unknown>
  loading: boolean
  load: () => Promise<void>
  save: (patch: Record<string, unknown>) => Promise<void>
}

export const useSettingsStore = create<SettingsState>((set) => ({
  settings: {},
  loading: false,
  load: async () => {
    set({ loading: true })
    try {
      const { data } = await api.get('/settings')
      set({ settings: data, loading: false })
    } catch {
      set({ loading: false })
    }
  },
  save: async (patch) => {
    const { data } = await api.put('/settings', patch)
    set((s) => ({ settings: { ...s.settings, ...data.updated } }))
  },
}))
