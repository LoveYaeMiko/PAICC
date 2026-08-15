import axios from 'axios'

export const BACKEND_URL =
  window.paicc?.backendUrl ?? import.meta.env.VITE_BACKEND_URL ?? 'http://127.0.0.1:8000'

export const WS_URL = `${BACKEND_URL.replace(/^http/, 'ws')}/ws/events`

export const api = axios.create({
  baseURL: `${BACKEND_URL}/api`,
  timeout: 30000,
})

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 428) {
      // 428 Precondition Required → a confirmation is needed.
      err.isConfirmationRequired = true
    }
    return Promise.reject(err)
  },
)
