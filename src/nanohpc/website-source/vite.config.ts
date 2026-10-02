import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { docsMarkdown, policyMarkdown, templateValues } from './src/documentation.ts'

/** Emit docs.md and policy.md as templates; the deploy replaces each {{name}} with the value from site.json. */
function markdownDocuments(): Plugin {
  return {
    name: 'markdown-documents',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'docs.md', source: docsMarkdown(templateValues) })
      this.emitFile({ type: 'asset', fileName: 'policy.md', source: policyMarkdown(templateValues) })
    },
  }
}

// Relative asset URLs and hash routing let the same build work under any path, such as /cluster/ or /.
export default defineConfig({
  base: './',
  plugins: [react(), markdownDocuments()],
  build: { sourcemap: false, outDir: '../website', emptyOutDir: true },
})
