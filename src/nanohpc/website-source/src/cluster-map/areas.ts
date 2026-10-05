/** The three cluster map groupings. */
export type LayoutName = 'default' | 'partitions' | 'geographic'

export type Area<T> = { label: string; front: boolean; members: T[] }
type Placed = { front: boolean; building: string | null; partitions: string[] }

/** Group machines into labeled areas while preserving snapshot order. */
export function mapAreas<T extends Placed>(machines: T[], layout: LayoutName): Area<T>[] {
  const label = (machine: T): string => (layout === 'partitions'
    ? machine.partitions.length ? [...machine.partitions].sort().join(' + ') : 'No partition'
    : layout === 'geographic' ? machine.building ?? 'Unknown building' : 'Compute').toUpperCase()
  const front = machines.filter(machine => machine.front)
  const areas: Area<T>[] = front.length ? [{ label: 'FRONT NODE', front: true, members: front }] : []
  for (const machine of machines.filter(item => !item.front)) {
    const existing = areas.find(area => !area.front && area.label === label(machine))
    if (existing) existing.members.push(machine)
    else areas.push({ label: label(machine), front: false, members: [machine] })
  }
  return areas
}
