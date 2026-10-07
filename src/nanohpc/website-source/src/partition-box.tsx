import { partitionTime, type Partition } from './guide'
import { gpuModels, type Machine } from './machines'

type Policy = { name: string; value: string }

/** Align a compact text table without hiding live policy values. */
function textTable(rows: string[][], right: number[]): string {
  const widths = rows[0].map((_, column) => Math.max(...rows.map(row => row[column].length)))
  return rows.map(row => row.map((cell, column) =>
    right.includes(column) ? cell.padStart(widths[column]) : cell.padEnd(widths[column])).join('  ').trimEnd()).join('\n')
}

/** Show a Slurm GPU limit in plain words, or retain a value we cannot simplify. */
function gpuLimit(value: string | undefined): string {
  if (value === undefined) return 'no limit'
  const match = /(?:^|,)gres\/gpu=(\d+)(?:,|$)/.exec(value)
  if (!match) return value
  return match[1] + (match[1] === '1' ? ' GPU' : ' GPUs')
}

/** Render observed partitions, limits, and machine models in desktop and phone widths. */
function boxText(partitions: Partition[], nodes: Machine[], policies: Policy[], short: boolean): string {
  const value = (name: string): string | undefined => policies.find(policy => policy.name === name)?.value
  const members = (partition: Partition): string[] => partition.nodes.split(',').filter(Boolean)
  const partitionRows = partitions.map(partition => {
    const gpus = nodes.filter(node => members(partition).includes(node.name)).reduce((sum, node) => sum + (node.total_gpus ?? 0), 0)
    return [
      partition.name + (partition.default ? ' (default)' : ''),
      partitionTime(partition.max_time),
      String(gpus),
      gpuLimit(value(partition.name + ': GPUs per job (most)')),
      gpuLimit(value(partition.name + ': simultaneous resources per user')),
    ]
  })
  const machineRows = nodes.filter(node => node.role === 'Compute').map(node => [
    node.name,
    partitions.filter(partition => members(partition).includes(node.name)).map(partition => partition.name).join(', ') || 'none',
    gpuModels(node.specs),
  ])
  const columns = short ? 3 : 5
  return [
    '# Live from Slurm. On the front node: sinfo --summarize',
    '',
    textTable([['PARTITION', 'TIME LIMIT', 'GPUS', 'MOST PER JOB', 'PER USER'], ...partitionRows].map(row => row.slice(0, columns)), [2, 3]),
    '',
    textTable([['MACHINE', 'PARTITIONS', 'GPUS'], ...machineRows], []),
  ].join('\n')
}

/** Present the same live partition summary with fewer columns on phones. */
export function PartitionBox({ partitions, nodes, policies }: { partitions: Partition[]; nodes: Machine[]; policies: Policy[] }) {
  return <section className="partition-summary"><h2>Partitions</h2><pre>
    <code className="partition-wide">{boxText(partitions, nodes, policies, false)}</code>
    <code className="partition-short">{boxText(partitions, nodes, policies, true)}</code>
  </pre></section>
}
