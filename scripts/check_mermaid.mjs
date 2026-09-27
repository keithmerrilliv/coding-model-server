// Parse every ```mermaid block in docs/*.md and README.md; exit 1 on any
// parse error. Syntax only — nothing is rendered.
//
//   node scripts/check_mermaid.mjs            # checks this checkout
//   node scripts/check_mermaid.mjs <repo>     # checks another checkout
//
// Uses the mermaid the dashboard already installs (cd dashboard && npm ci).
// mermaid.parse wants DOMPurify's hooks, and Node has no DOM; stubbing them
// affects sanitising only, not parsing.
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const modules = path.join(here, '..', 'dashboard', 'node_modules');
const repo = path.resolve(process.argv[2] || path.join(here, '..'));

const dp = (await import(path.join(modules, 'dompurify/dist/purify.es.mjs'))).default;
dp.addHook = () => {}; dp.removeHook = () => {}; dp.removeAllHooks = () => {};
dp.sanitize = (x) => x;
const mermaid = (await import(path.join(modules, 'mermaid/dist/mermaid.core.mjs'))).default;
mermaid.initialize({ startOnLoad: false });

const docs = path.join(repo, 'docs');
const files = [path.join(repo, 'README.md'),
  ...fs.readdirSync(docs).filter((f) => f.endsWith('.md')).map((f) => path.join(docs, f))];
let blocks = 0, bad = 0;
for (const f of files) {
  const text = fs.readFileSync(f, 'utf8');
  const re = /```mermaid\n([\s\S]*?)```/g;
  let m, i = 0;
  while ((m = re.exec(text))) {
    i++; blocks++;
    try { await mermaid.parse(m[1]); }
    catch (e) {
      bad++;
      console.log(`FAIL ${path.relative(repo, f)} block ${i}: ${String(e.message || e).slice(0, 400)}`);
    }
  }
}
console.log(`${blocks - bad}/${blocks} mermaid blocks parse`);
process.exit(bad ? 1 : 0);
