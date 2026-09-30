import { createContext, useContext, useState } from 'react'
import { clusterKey } from './pages/_picker.jsx'

const Context = createContext(null)
const storageKey = 'torre.selectedCluster'
function savedKey() {
  try { return sessionStorage.getItem(storageKey) } catch { return null }
}

export function ClusterContext({ clusters, children }) {
  const [key, setKey] = useState(savedKey)
  const selected = key === '' ? null : clusters.find(c => clusterKey(c) === key) || clusters[0] || null
  const select = c => {
    const next = clusterKey(c)
    setKey(next)
    try { sessionStorage.setItem(storageKey, next) } catch { /* memory-only state */ }
  }
  return <Context.Provider value={{ selected, select, clusters }}>{children}</Context.Provider>
}

export function useClusterSelection(general = false) {
  const { selected, select, clusters } = useContext(Context)
  return [general ? selected : selected || clusters[0], select]
}
