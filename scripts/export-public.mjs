import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Export reviewed source into a new tree; never copy private Git/Sites history.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const entries = ['README.md', 'RESUME.md', 'SECURITY.md', '.env.example', '.gitignore', '.railwayignore', 'Dockerfile',
  'requirements.txt', 'railway.json', 'package.json', 'src', 'tests', 'showcase', 'docs',
  'alpha-atlas/web', 'alpha-atlas/tests/data-contract.mjs', 'scripts/export-public.mjs', 'scripts/create-dashboard-auth.py'];
const blocked = new Set(['.git', '.openai', '.sites-runtime', 'node_modules', '__pycache__', '.pytest_cache']);
const checks = [
  ['telegram-token', /\b\d{8,}:[A-Za-z0-9_-]{25,}\b/],
  ['api-key', /\bsk-(?:teamo-)?[A-Za-z0-9]{28,}\b/],
  ['github-token', /\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b/],
  ['private-channel-id', /-100\d{9,}/],
  ['private-key', /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/],
];
const files = [];
async function inspect(relative) {
  const absolute = path.join(root, relative);
  const stat = await fs.lstat(absolute);
  if (stat.isSymbolicLink()) throw new Error(`Symlinks require review: ${relative}`);
  if (stat.isDirectory()) {
    for (const child of (await fs.readdir(absolute)).sort()) {
      if (!blocked.has(child) && !/\.(?:pyc|db|sqlite3?|tar|gz|zip)$/.test(child)) {
        await inspect(path.join(relative, child));
      }
    }
    return;
  }
  if (!stat.isFile()) return;
  const content = await fs.readFile(absolute);
  const decoded = content.toString('utf8');
  for (const [kind, pattern] of checks) {
    if (pattern.test(decoded)) throw new Error(`Public export stopped: ${kind} in ${relative}`);
  }
  files.push({ relative, absolute });
}
for (const entry of entries) await inspect(entry);
const releaseRoot = path.join(root, 'public-release');
await fs.mkdir(releaseRoot, { recursive: true });
const destination = await fs.mkdtemp(path.join(releaseRoot, 'alpha-atlas-'));
for (const file of files) {
  const target = path.join(destination, file.relative);
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.copyFile(file.absolute, target);
}
console.log(JSON.stringify({ destination, files: files.length, secretScan: 'passed', oldHistoryCopied: false }));
