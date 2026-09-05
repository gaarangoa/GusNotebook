import {build} from 'esbuild';
import {mkdir, copyFile, readFile, writeFile, readdir} from 'node:fs/promises';

const destination = 'src/gusnotebook/static/vendor';
const lock = JSON.parse(await readFile('package-lock.json', 'utf8'));
for (const singleton of ['@codemirror/state', '@codemirror/view']) {
  if (Object.keys(lock.packages).some(path => path.endsWith(`/node_modules/${singleton}`))) {
    throw new Error(`Deduplicate ${singleton} before building: multiple copies break editor history`);
  }
}
await mkdir(destination, {recursive: true});
for (const entry of ['vendor', 'codemirror']) {
  await build({entryPoints: [`frontend/${entry}.js`], bundle: true, minify: true,
    format: entry === 'vendor' ? 'iife' : 'esm', target: 'es2022',
    outfile: `${destination}/${entry}.js`, legalComments: 'eof'});
}
await copyFile('node_modules/@xterm/xterm/css/xterm.css', `${destination}/xterm.css`);
const notices = [];
for (const [path, info] of Object.entries(lock.packages)) {
  if (!path || info.dev || info.optional) continue;
  for (const filename of await readdir(path)) {
    if (/^(licen[sc]e|copying|notice)(\.|$)/i.test(filename)) {
      notices.push(`${path} ${info.version}\n${await readFile(`${path}/${filename}`, 'utf8')}`);
    }
  }
}
await writeFile(`${destination}/LICENSES.txt`, notices.join('\n\n---\n\n'));
