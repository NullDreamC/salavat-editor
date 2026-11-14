import { createRoot } from 'react-dom/client'
import { App } from './App'
import './style.css'

createRoot(document.getElementById('root')!).render(<App />)

// Telegram supplies launch parameters; account permissions still come from server login.
if (window.location.hash.includes('tgWebApp')) {
  const script = document.createElement('script')
  script.src = 'https://telegram.org/js/telegram-web-app.js'
  script.onload = () => {
    const telegram = (window as unknown as { Telegram?: { WebApp: { ready: () => void; expand: () => void } } }).Telegram
    telegram?.WebApp.ready()
    telegram?.WebApp.expand()
  }
  document.head.appendChild(script)
}
