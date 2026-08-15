import { ConfigProvider, theme } from 'antd'
import { HashRouter, Route, Routes } from 'react-router-dom'
import MainLayout from './layout/MainLayout'
import SystemPage from './pages/SystemPage'
import FilesPage from './pages/FilesPage'
import AppsPage from './pages/AppsPage'
import StoragePage from './pages/StoragePage'
import QuantPage from './pages/QuantPage'
import ResearchPage from './pages/ResearchPage'
import ChatPage from './pages/ChatPage'
import ClaudePage from './pages/ClaudePage'
import SettingsPage from './pages/SettingsPage'

export default function App(): JSX.Element {
  return (
    <ConfigProvider
      theme={{
        algorithm: theme.darkAlgorithm,
        token: { colorPrimary: '#4a90e2', borderRadius: 8 },
      }}
    >
      <HashRouter>
        <Routes>
          <Route element={<MainLayout />}>
            <Route index element={<SystemPage />} />
            <Route path="/files" element={<FilesPage />} />
            <Route path="/apps" element={<AppsPage />} />
            <Route path="/storage" element={<StoragePage />} />
            <Route path="/quant" element={<QuantPage />} />
            <Route path="/research" element={<ResearchPage />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/claude" element={<ClaudePage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Route>
        </Routes>
      </HashRouter>
    </ConfigProvider>
  )
}
