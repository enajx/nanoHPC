import { useState } from 'react'
import { Check, Clipboard } from 'lucide-react'
import { guidePanels, guideSteps, panelIntros, guideTopics, type GuideBlock, type GuidePanel, type GuideTopic, type GuideValues } from './documentation'

export type Partition = { name: string; default: boolean; max_time: string; nodes: string }

/** Show whole-hour Slurm partition limits in plain words. */
export function partitionTime(value: string): string {
  const match = /^(?:(\d+)-)?(\d+):(\d+):(\d+)$/.exec(value)
  if (!match || Number(match[3]) || Number(match[4])) return value
  const hours = Number(match[1] ?? 0) * 24 + Number(match[2])
  return `${hours} ${hours === 1 ? 'hour' : 'hours'}`
}

/** The live partition summary: name, default, time limit, and machines. */
export function PartitionSummary({ partitions }: { partitions: Partition[] | null }) {
  if (!partitions) return <p role="status">Partitions are unavailable until the monitoring data loads.</p>
  return <>{partitions.map((partition) => <p key={partition.name}>{partition.name}{partition.default ? ' (default)' : ''}: {partitionTime(partition.max_time)}, {partition.nodes}</p>)}</>
}

/** Render one canonical guide block in the styled website. */
function GuideContentBlock({ block, partitions }: { block: GuideBlock; partitions: Partition[] | null }) {
  if (block.kind === 'partitions') return <div><h3>Partitions on this cluster</h3><PartitionSummary partitions={partitions}/></div>
  if (block.kind === 'heading') return <h3>{block.text}</h3>
  if (block.kind === 'paragraph') return <p>{block.text}</p>
  if (block.kind === 'code') return <pre><code>{block.text}</code></pre>
  if (block.kind === 'links') return <p>{block.items.map((item, index) => <span key={item.href}>{index > 0 && ' · '}<a href={item.href}>{item.label}</a></span>)}</p>
  return <ul className="job-downloads">{block.items.map((item) => <li key={item.file}><a href={`job-examples/${item.file}`} download={item.file}>Download {item.file}</a>: {item.description}</li>)}</ul>
}

/** Show the setup and submission steps as cards in two columns; order keeps list order when the columns merge. */
function GuideSteps({ values, partitions }: { values: GuideValues; partitions: Partition[] | null }) {
  return <div className="setup-cards">{(['left', 'right'] as const).map((column) => <div key={column} className="setup-column">
    {guideSteps(values).map((step, order) => step.column === column && <section key={step.title} className="instruction panel" style={{ order }}><h2>{step.title}</h2>
      {step.blocks.map((block, index) => <GuideContentBlock key={`${block.kind}-${index}`} block={block} partitions={partitions}/>)}</section>)}</div>)}</div>
}

/** Support pointer and keyboard selection in either row of tabs. */
function TabBar({ labels, selected, select, name, prefix }: { labels: string[]; selected: number; select: (index: number) => void; name: string; prefix: string }) {
  return <div className="guide-tabs" role="tablist" aria-label={name}>{labels.map((label, index) =>
    <button key={label} type="button" role="tab" id={`${prefix}-tab-${index}`} aria-controls={`${prefix}-panel-${index}`} aria-selected={selected === index} tabIndex={selected === index ? 0 : -1}
      onClick={() => select(index)} onKeyDown={(event) => {
        const next = event.key === 'ArrowRight' ? (index + 1) % labels.length : event.key === 'ArrowLeft' ? (index + labels.length - 1) % labels.length : event.key === 'Home' ? 0 : event.key === 'End' ? labels.length - 1 : null
        if (next === null) return
        event.preventDefault(); select(next); document.getElementById(`${prefix}-tab-${next}`)?.focus()
      }}>{label}</button>)}</div>
}

/** Copy only the selected example and report clipboard failures. */
function CopyCode({ command }: { command: string }) {
  const [state, setState] = useState<'ready' | 'copied' | 'failed'>('ready')
  return <div className="tab-code"><pre><code>{command}</code></pre>
    <button type="button" className="copy-code" aria-label={state === 'copied' ? 'Copied' : 'Copy code'} onClick={() => {
      navigator.clipboard.writeText(command).then(() => setState('copied')).catch(() => setState('failed'))
    }}>{state === 'copied' ? <Check size={23}/> : <Clipboard size={23}/>}</button>
    {state === 'failed' && <p role="alert">Copy failed. Select and copy the code.</p>}
  </div>
}

/** Show one panel's topics as tabs, with the selected topic's text and code. */
function TopicTabs({ panel, prefix, allTopics }: { panel: GuidePanel; prefix: string; allTopics: GuideTopic[] }) {
  const [topic, setTopic] = useState(0)
  const topics = allTopics.filter(item => item.panel === panel)
  const selected = topics[topic]
  return <div className="optional-tabs panel">
    <TabBar labels={topics.map(item => item.title)} selected={topic} select={setTopic} name={panel} prefix={prefix}/>
    <div role="tabpanel" id={`${prefix}-panel-${topic}`} aria-labelledby={`${prefix}-tab-${topic}`}>
      <p className="topic-text">{selected.text}</p>
      {selected.whenToUse && <p className="topic-text topic-use"><strong>When to use:</strong> {selected.whenToUse}</p>}
      <CopyCode key={selected.title} command={selected.command}/>
    </div>
  </div>
}

/** Render the card-and-tab cluster guide with this cluster's values and live partitions. */
export function HowToUse({ values, partitions }: { values: GuideValues; partitions: Partition[] | null }) {
  const topics = guideTopics(values)
  return <div className="user-guide guide-alt">
    <GuideSteps values={values} partitions={partitions}/>
    {guidePanels.map((panel, index) => <section key={panel}><h2 className="reference-heading">{panel}</h2>{panelIntros[panel] && <p className="panel-intro">{panelIntros[panel]}</p>}<TopicTabs panel={panel} prefix={`panel-${index}`} allTopics={topics}/></section>)}
  </div>
}
