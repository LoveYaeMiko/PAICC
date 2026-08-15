import React from 'react'
import ReactDOM from 'react-dom/client'
import 'antd/dist/reset.css'
import './assets/global.css'
import App from './App'
import BallApp from './BallApp'

const view =
  window.paicc?.view ?? new URLSearchParams(window.location.search).get('view') ?? 'main'

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>{view === 'ball' ? <BallApp /> : <App />}</React.StrictMode>,
)
