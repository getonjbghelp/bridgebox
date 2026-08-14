import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/global.css'
import App from './App.tsx'
import { MotionPrefsProvider } from './state/MotionPrefsContext'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MotionPrefsProvider>
      <App />
    </MotionPrefsProvider>
  </StrictMode>,
)
