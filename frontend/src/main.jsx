import './polyfills.js'
import React from 'react'
import ReactDOM from 'react-dom/client'
import LeafyGreenProvider from '@leafygreen-ui/leafygreen-provider'
import App from './App.jsx'
import './styles.css'
import './pov-signature.css'

// LeafyGreenProvider with darkMode — authentic MongoDB Atlas look
ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <LeafyGreenProvider darkMode>
      <App />
    </LeafyGreenProvider>
  </React.StrictMode>,
)
