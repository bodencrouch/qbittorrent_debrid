import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { EmbedHost } from './embed/EmbedHost'
import './index.css'

const params = new URLSearchParams(window.location.search)
const panel = params.get('panel')

if (!panel) {
  // Standalone shell: index.html no longer hardcodes the dark class (the
  // embedded bundle needs to follow the host's theme instead), so restore
  // today's look explicitly here.
  document.documentElement.classList.add('dark')
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {panel ? (
      <EmbedHost
        initialPanel={panel}
        initialHash={params.get('hash')}
        initialTheme={params.get('theme')}
        initialSection={params.get('section')}
      />
    ) : (
      <App />
    )}
  </React.StrictMode>,
)
