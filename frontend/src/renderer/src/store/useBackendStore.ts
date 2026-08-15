import { create } from 'zustand'

interface BackendState {
  ready: boolean
  url: string
  setStatus: (s: { ready: boolean; url: string }) => void
}

export const useBackendStore = create<BackendState>((set) => ({
  ready: false,
  url: '',
  setStatus: (s) => set(s),
}))
