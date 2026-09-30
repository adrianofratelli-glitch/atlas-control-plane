// Reusable cluster selector (native select styled MongoDB-like)
// Keyed by project_id + cluster_name: "Cluster0" is Atlas's default name, so
// the same cluster name easily repeats across projects in a real org.
export const clusterKey = (c) => (c ? `${c.project_id}:${c.cluster_name}` : '')

export function ClusterPicker({ clusters, value, onChange }) {
  return (
    <select
      className="mono"
      value={clusterKey(value)}
      onChange={e => onChange(clusters.find(c => clusterKey(c) === e.target.value))}
      style={{
        background: 'var(--bg-card)', color: 'var(--text-pri)', border: '1px solid var(--border-accent)',
        borderRadius: 6, padding: '8px 12px', fontSize: 13, minWidth: 220,
      }}
    >
      {clusters.map(c => (
        <option key={clusterKey(c)} value={clusterKey(c)}>
          {c.project_name} / {c.cluster_name} ({c.tier})
        </option>
      ))}
    </select>
  )
}
