import { create } from 'zustand'
import type { SystemStats, ProcessInfo } from '@/types'

interface SystemState {
  stats: SystemStats | null
  processes: ProcessInfo[]
  setStats: (s: SystemStats) => void
  setProcesses: (p: ProcessInfo[]) => void
}

export const useSystemStore = create<SystemState>((set) => ({
  stats: null,
  processes: [],
  setStats: (stats) => set({ stats }),
  setProcesses: (processes) => set({ processes }),
}))
