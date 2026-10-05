/** Canonical content for the website guide, policy explanation, and Markdown copies. */
import type { SiteSettings } from './site'

export type GuideBlock =
  | { kind: 'heading'; text: string }
  | { kind: 'paragraph'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'pair'; do: string; dont: string }
  | { kind: 'links'; items: { label: string; href: string }[] }
  | { kind: 'downloads'; items: { file: string; description: string }[] }
  // The cluster's partitions: live from status.json on the page, a pointer to the policy page in Markdown.
  | { kind: 'partitions' }

/** Column is the desktop column; narrow screens show steps in list order. */
export type GuideStep = { title: string; column: 'left' | 'right'; blocks: GuideBlock[] }
/** The How to page shows one tabbed panel per entry, in this order. */
export const guidePanels = ['Batch vs interactive', 'Shared vs scratch', 'Other settings', 'Jobs examples'] as const
export type GuidePanel = typeof guidePanels[number]
/** Short text shown under a panel's heading, before its tabs. */
export const panelIntros: Partial<Record<GuidePanel, string>> = {
  'Shared vs scratch': 'We recommend shared mode with a git worktree for most jobs. Use scratch mode for jobs that read or write a lot.',
}
export type GuideTopic = { title: string; panel: GuidePanel; group?: string; text: string; whenToUse?: string; command: string }

/** The cluster's own values in the guide, as text. */
export type GuideValues = {
  cluster_name: string
  login_address: string
  home_quota_soft_gb: string
  home_quota_hard_gb: string
  scratch_cleanup_days: string
}

/** Placeholders for docs.md and policy.md; the deploy replaces each {{name}} with the value from site.json. */
export const templateValues: GuideValues = {
  cluster_name: '{{cluster_name}}',
  login_address: '{{login_address}}',
  home_quota_soft_gb: '{{home_quota_soft_gb}}',
  home_quota_hard_gb: '{{home_quota_hard_gb}}',
  scratch_cleanup_days: '{{scratch_cleanup_days}}',
}

/** The page's values, from site.json. */
export function siteValues(site: SiteSettings): GuideValues {
  return {
    cluster_name: site.cluster_name,
    login_address: site.login_address,
    home_quota_soft_gb: String(site.home_quota_soft_gb),
    home_quota_hard_gb: String(site.home_quota_hard_gb),
    scratch_cleanup_days: String(site.scratch_cleanup_days),
  }
}

/** The setup and submission steps, filled with the cluster's values. */
export function guideSteps(values: GuideValues): GuideStep[] {
  const name = values.cluster_name
  return [
    { title: '0. Setup', column: 'left', blocks: [
      { kind: 'heading', text: `SSH access to ${name}` },
      { kind: 'paragraph', text: 'On your laptop, use an existing SSH key or create one at an unused path:' },
      { kind: 'code', text: `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_${name}` },
      { kind: 'paragraph', text: `Send the public key (.pub) to the cluster admin and ask for an account on the ${name} front node. Keep the private key on your laptop. Add your assigned username and key path to ~/.ssh/config:` },
      { kind: 'code', text: `Host ${name}\n    HostName ${values.login_address}\n    User USER\n    IdentityFile ~/.ssh/id_ed25519_${name}` },
    ] },
    { title: `1. SSH into ${name}`, column: 'right', blocks: [
      { kind: 'code', text: `ssh -A ${name}` },
      { kind: 'heading', text: `Get your code onto ${name}` },
      { kind: 'paragraph', text: `Forward your laptop’s SSH agent, then clone or pull from GitHub. This uses your loaded laptop key without copying its private key to ${name}. Only forward your agent to a machine you trust. (Other options: a read-only deploy key for one repository, or gh auth login on ${name}.)` },
      { kind: 'code', text: 'git clone git@github.com:OWNER/REPOSITORY.git\ncd REPOSITORY' },
    ] },
    { title: '2. Submit a job', column: 'right', blocks: [
      { kind: 'paragraph', text: 'A job script asks Slurm for resources, then runs your commands. A job goes to the default partition unless you name another one with --partition. Each partition has a maximum run time; if you leave out --time, a job gets that maximum. Default memory per CPU and CPUs per GPU are also set per partition, and memory is enforced.' },
      { kind: 'partitions' },
      { kind: 'heading', text: 'job.sh: save this file in your project' },
      { kind: 'code', text: '#!/bin/bash\n#SBATCH --gpus=1\n#SBATCH --time=00:05:00\n\nset -euo pipefail\nuv run --locked python script.py' },
      { kind: 'paragraph', text: 'set -euo pipefail stops the script when a command fails, an unset variable is used, or a command in a pipeline fails. Replace uv run --locked python script.py with the command that runs your code. uv is installed on the front node and the compute machines. If jobs have outbound internet access, uv can download locked packages; downloads use job time. Use nvidia-smi when you only want to check the GPU assigned to the job.' },
      { kind: 'heading', text: 'Terminal: type these commands after saving job.sh' },
      { kind: 'paragraph', text: 'We recommend submitting from a git worktree: a separate checkout of one commit, so editing or pulling in your project does not change the code of a waiting or running job. The worktree holds the last commit only, so commit job.sh and your code first, and add .worktrees/ and logs/ to .gitignore.' },
      { kind: 'code', text: 'cd ~/YOUR_PROJECT\nmkdir -p logs\nRUN=.worktrees/$(date +%Y%m%d-%H%M%S)\ngit worktree add --detach "$RUN"\n(cd "$RUN" && sbatch --output="$HOME/YOUR_PROJECT/logs/job-%j.log" job.sh)\nsqueue --me' },
      { kind: 'paragraph', text: 'Write results to an absolute path in your home, such as $HOME/YOUR_PROJECT/results, not inside the worktree. When the job has ended, remove the worktree from your project folder with git worktree remove "$RUN" (git worktree list shows them all).' },
      { kind: 'paragraph', text: `/home is shared between ${name} and the compute machines. Your home has a soft quota of ${values.home_quota_soft_gb} GB and a hard quota of ${values.home_quota_hard_gb} GB: you can go above the soft quota for a limited time, never above the hard quota. sbatch reads and writes the files in the folder you submit from. /scratch/$USER is fast local temporary storage; for jobs that read or write a lot, use cluster-submit --mode=scratch job.sh to let the cluster manage a private copy on the compute machine. Both use Slurm and follow the same resource requests.` },
      { kind: 'paragraph', text: 'Do not name a node or use --constraint for normal jobs. Slurm selects a suitable compute machine.' },
      { kind: 'links', items: [
        { label: 'Current machines', href: 'machines.md' },
        { label: 'Cluster policy', href: 'policy.md' },
      ] },
    ] },
    { title: 'Example job scripts', column: 'left', blocks: [
      { kind: 'downloads', items: [
        { file: 'gpu-check-job.sh', description: 'one GPU, checks the assigned GPU.' },
        { file: 'one-gpu-uv-job.sh', description: 'one GPU, runs a uv Python script.' },
        { file: 'four-gpu-torchrun-job.sh', description: 'four GPUs, runs PyTorch distributed training.' },
        { file: 'cpu-memory-job.sh', description: 'one GPU with explicit CPU and RAM requests.' },
      ] },
    ] },
    { title: "Do's and don'ts", column: 'left', blocks: [
      { kind: 'pair', do: 'run installs and interactive tools inside a batch job or an interactive shell on a compute machine.',
        dont: `run heavy or long-lived work on ${name}; the front node is shared by everyone.` },
    ] },
  ]
}

/** The tabbed reference topics, filled with the cluster's values. */
export function guideTopics(values: GuideValues): GuideTopic[] {
  return [
    { title: 'Batch job', panel: 'Batch vs interactive', text: 'Save your commands in a job script and submit it with sbatch, preferably from a git worktree (see Shared vs scratch). Slurm starts it when the requested resources are free, in the default partition unless you name another one, and stops it at its time limit. You can log out while it waits or runs; its output goes to the file named by --output.', whenToUse: 'training runs, sweeps, and anything that runs without you watching it.', command: 'cd ~/YOUR_PROJECT/.worktrees/RUN\nsbatch job.sh\nsqueue --me' },
    { title: 'Interactive shell', panel: 'Batch vs interactive', text: 'Partitions set up for interactive shells accept only this command form. Replace PARTITION with such a partition; the Cluster policy page lists the partitions. You can change the resource amounts and time. Slurm waits for those resources, selects the compute node, and opens Bash there. Exit the shell to release the resources. Direct SSH to compute nodes is for administrators only.', whenToUse: 'debugging, short tests, and checking that your code and environment work on a GPU before you submit a long batch job.', command: 'srun --partition=PARTITION --gpus=1 --cpus-per-task=4 --time=01:00:00 --pty bash -l' },
    { title: 'With a git worktree', panel: 'Shared vs scratch', group: 'Shared mode', text: 'Make a separate git worktree for each run and submit from it with sbatch. The job runs that fixed copy of the code, so you can keep editing and pulling in your main checkout. The worktree holds the last commit only, so commit your changes first, and add .worktrees/ and logs/ to .gitignore. Write results to an absolute path in your home outside the worktree, such as $HOME/YOUR_PROJECT/results: they are saved as the job writes them, and a job stopped at its time limit loses only the work in progress. Cost: each worktree is a full copy of the code in your home, and uv sync builds a separate .venv in it. The job reads both over the network, and building the .venv copies the whole environment into your home, which takes job time for large packages such as PyTorch. Remove the worktree when the job has ended; git worktree remove refuses if it holds files you have not committed.', whenToUse: 'most jobs: training runs and sweeps, especially long ones or many at once while you keep changing the code.', command: 'cd ~/YOUR_PROJECT\nmkdir -p logs\nRUN=.worktrees/$(date +%Y%m%d-%H%M%S)\ngit worktree add --detach "$RUN"\n(cd "$RUN" && sbatch --output="$HOME/YOUR_PROJECT/logs/job-%j.log" job.sh)\n\n# After the job has ended:\ngit worktree remove "$RUN"' },
    { title: 'Plain', panel: 'Shared vs scratch', group: 'Shared mode', text: 'Use this when you want the job to read and write the shared project directly. There is no project copy or output copy-back. A git pull, branch switch, or file edit can change code used by processes that start later in a pending or running job.', whenToUse: 'jobs that spend their time on CPU or GPU work and read or write few files, when you will not edit the project while the job waits or runs.', command: 'cd ~/YOUR_PROJECT\nsbatch job.sh' },
    { title: 'With local scratch', panel: 'Shared vs scratch', group: 'Shared mode', text: `Keep code and results in your home, and put large datasets and temporary files on the compute machine’s local disk. Add these lines to job.sh. stage-dataset --private copies a dataset folder from your home (path relative to your home) to local scratch once, and later jobs on the same machine reuse it. A staged copy that no job has used for ${values.scratch_cleanup_days} days is deleted automatically. Files in TMPDIR are not copied back; the last line deletes them.`, whenToUse: 'large datasets that are read many times, or jobs that write many temporary files, where reading and writing over the shared home would slow the job.', command: 'export TMPDIR=/scratch/$USER/tmp/$SLURM_JOB_ID\nmkdir -p "$TMPDIR"\nDATA=$(stage-dataset --private datasets/MY_DATASET)\nuv run --locked python train.py --data "$DATA" --output "$HOME/YOUR_PROJECT/results/$SLURM_JOB_ID"\nrm -rf "$TMPDIR"' },
    { title: 'Basic', panel: 'Shared vs scratch', group: 'Scratch mode', text: 'Use this when you want a private project copy while the job runs. cluster-submit copies Git-tracked files when the job starts, including uncommitted edits. Changes made while the job waits may be copied; later changes do not affect the running copy. Add #CLUSTER include= for other inputs and #CLUSTER copy-back= for results to return, including after an application failure. Git commands such as git rev-parse do not work in the scratch copy, which has no .git folder. --mode is required (--mode=shared is the same as plain sbatch). sbatch options go before the script in the --name=value form, for example cluster-submit --mode=scratch --output=$HOME/YOUR_PROJECT/logs/job-%j.log job.sh.', whenToUse: 'projects with many small files or heavy reading and writing inside the project folder, or when the job should use a fixed copy of the project including uncommitted edits.', command: 'cd ~/YOUR_PROJECT\ncluster-submit --mode=scratch job.sh' },
    { title: 'Results in home', panel: 'Shared vs scratch', group: 'Scratch mode', text: 'Run from a private scratch copy, but write results and checkpoints to an absolute path in your home. They are saved as the job writes them, so they do not depend on copy-back at the end of the job, and a time limit or crash loses only the work in progress. These paths need no #CLUSTER copy-back= line.', whenToUse: 'long scratch-mode runs whose checkpoints you cannot afford to lose.', command: '#!/bin/bash\n#SBATCH --gpus=1\n#SBATCH --time=12:00:00\n#SBATCH --output=training-%j.log\nset -euo pipefail\nuv run --locked python train.py --output "$HOME/YOUR_PROJECT/results/$SLURM_JOB_ID"\n\n# Submit with:\n# cd ~/YOUR_PROJECT\n# cluster-submit --mode=scratch job.sh' },
    { title: 'Inputs and outputs', panel: 'Shared vs scratch', group: 'Scratch mode', text: 'For scratch mode. Add these lines to the job script. Outputs return to the same relative path inside the original project. Choose a different output directory for each run. Non-Git projects need explicit inputs.', command: '#CLUSTER include=datasets/small-test\n#CLUSTER copy-back=results/' },
    { title: 'Recover partial results', panel: 'Shared vs scratch', group: 'Scratch mode', text: 'For scratch mode. Failed jobs attempt to return declared outputs and keep their scratch directory. Read the job log for the recovery path. Node loss or forced termination can prevent copy-back. Write important checkpoints to your experiment’s persistent output path during execution.', command: 'less training-JOB_ID.log' },
    { title: 'Inspect your jobs', panel: 'Other settings', text: 'squeue shows queued and running jobs. sacct includes completed jobs. sstat shows CPU and memory use so far. An overlapping job step can inspect the assigned GPUs. seff is not installed; use sacct and sstat. Terminal output files and application checkpoints are separate outputs.', command: 'squeue --me\nsacct --starttime today --format=JobID,JobName,State,Elapsed,AllocTRES\nsstat -j JOB_ID.batch --format=JobID,AveCPU,MaxRSS\nsrun --jobid=JOB_ID --overlap nvidia-smi\nless training-JOB_ID.log' },
    { title: 'Home space', panel: 'Other settings', text: `Your home is shared between the front and compute machines. Its soft quota is ${values.home_quota_soft_gb} GB and its hard quota is ${values.home_quota_hard_gb} GB. Keep large temporary data on local scratch.`, command: 'du -sh "$HOME"\ndu -h -d 1 "$HOME" | sort -h' },
    { title: 'GPU software', panel: 'Other settings', text: 'The NVIDIA driver is installed on GPU machines. Check the assigned GPU and available driver from inside a job. Use the Machines page for the GPU model and memory on each machine.', command: 'nvidia-smi' },
    { title: 'Caches', panel: 'Other settings', text: 'uv, Hugging Face, and PyTorch caches are placed on local scratch on compute machines so package downloads do not fill the shared home.', command: 'printf "uv: %s\\nHugging Face: %s\\nPyTorch: %s\\n" "$UV_CACHE_DIR" "$HF_HOME" "$TORCH_HOME"' },
    { title: 'One GPU', panel: 'Jobs examples', text: 'Submit with sbatch from a git worktree, as shown in Shared vs scratch. The job goes to the default partition. The program must accept --output, or change that argument to match your program. Results go to an absolute path in your home, so they are saved as the job writes them.', command: '#!/bin/bash\n#SBATCH --gpus=1\n#SBATCH --time=02:00:00\nset -euo pipefail\nuv run --locked python train.py --output "$HOME/YOUR_PROJECT/results/$SLURM_JOB_ID"' },
    { title: 'Four GPUs', panel: 'Jobs examples', text: 'For training code that already supports four GPU workers. Requesting four GPUs does not make single-GPU code use them. Submit this script with sbatch job.sh from the shared project.', command: '#!/bin/bash\n#SBATCH --gpus=4\n#SBATCH --cpus-per-task=16\n#SBATCH --time=02:00:00\n#SBATCH --output=training-%j.log\nset -euo pipefail\nuv run --locked torchrun --nproc-per-node=4 train.py' },
    { title: 'More CPUs and RAM', panel: 'Jobs examples', text: 'Request 8 CPUs and 64 GiB of RAM for one GPU. The request must fit on one compute machine; the Machines page lists each machine’s CPU cores and RAM. Submit with sbatch job.sh from the shared project.', command: '#!/bin/bash\n#SBATCH --gpus=1\n#SBATCH --cpus-per-task=8\n#SBATCH --mem=64G\n#SBATCH --time=02:00:00\n#SBATCH --output=training-%j.log\nset -euo pipefail\nuv run --locked python train.py' },
  ]
}

export const policyExplanation = {
  title: 'Queue ranking',
  text: 'GPU-hours are allocated GPU count multiplied by runtime. CPU and RAM do not contribute to fair-share usage. Recent usage decays over time; users with less recent usage receive a higher fair-share factor. Waiting time also contributes to priority, and jobs with a shorter requested time may receive a bonus. Running jobs are not interrupted by ranking, and priority does not guarantee a start time.',
  command: 'sshare -al\nsprio',
  liveSummary: 'The partition names, default partition, time limits, and machines are live values. Open the website policy page for the current partition summary.',
}

function markdownCode(value: string): string {
  return `\`\`\`bash\n${value}\n\`\`\``
}

function markdownBlock(block: GuideBlock): string {
  if (block.kind === 'heading') return `### ${block.text}`
  if (block.kind === 'paragraph') return block.text
  if (block.kind === 'code') return markdownCode(block.text)
  if (block.kind === 'pair') return `Do: ${block.do}\n\nDon't: ${block.dont}`
  if (block.kind === 'links') return block.items.map((item) => `[${item.label}](${item.href})`).join(' · ')
  if (block.kind === 'partitions') return 'The partitions, their time limits, and their machines are live values: see them on the [Cluster policy page](./#policy).'
  return block.items.map((item) => `- [Download ${item.file}](job-examples/${item.file}): ${item.description}`).join('\n')
}

/** Generate the plain Markdown view from the same content rendered by the guide. Links are relative to the page. */
export function docsMarkdown(values: GuideValues): string {
  const steps = guideSteps(values).map((step) => `## ${step.title}\n\n${step.blocks.map(markdownBlock).join('\n\n')}`).join('\n\n')
  const topics = guidePanels.map((panel) => {
    const selected = guideTopics(values).filter((topic) => topic.panel === panel)
    const intro = panelIntros[panel] ? `${panelIntros[panel]}\n\n` : ''
    return `## ${panel}\n\n${intro}${selected.map((topic) => `### ${topic.group ? `${topic.group}: ` : ''}${topic.title}\n\n${topic.text}${topic.whenToUse ? `\n\nWhen to use: ${topic.whenToUse}` : ''}\n\n${markdownCode(topic.command)}`).join('\n\n')}`
  }).join('\n\n')
  return `# ${values.cluster_name} documentation\n\n> Plain Markdown copy of the How to page.\n\n[Open the website view](./#docs)\n\n${steps}\n\n${topics}\n`
}

/** Generate the static policy explanation, with the changing partition data left live. */
export function policyMarkdown(values: GuideValues): string {
  return `# ${values.cluster_name} cluster policy\n\n> Static explanation of queue policy. The partition summary changes with Slurm, so read it on the website.\n\n[Open the live policy page](./#policy)\n\n## ${policyExplanation.title}\n\n${policyExplanation.text}\n\n${markdownCode(policyExplanation.command)}\n\n## Live partition summary\n\n${policyExplanation.liveSummary}\n`
}
