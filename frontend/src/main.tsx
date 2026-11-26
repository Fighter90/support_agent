import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Store from './Store'
import Manager from './Manager'
import PayDemo from './PayDemo'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/manager/*" element={<Manager />} />
        <Route path="/pay/demo/:id" element={<PayDemo />} />
        <Route path="*" element={<Store />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
