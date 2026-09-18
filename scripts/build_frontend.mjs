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
await copyFile('node_modules/katex/dist/katex.min.css', `${destination}/katex.css`);
await mkdir(`${destination}/fonts`, {recursive: true});
for (const filename of await readdir('node_modules/katex/dist/fonts')) {
  await copyFile(`node_modules/katex/dist/fonts/${filename}`, `${destination}/fonts/${filename}`);
}
for (const face of ['Regular', 'Italic', 'SemiBold', 'SemiBoldItalic']) {
  const filename = `IBMPlexMono-${face}.woff2`;
  await copyFile(`node_modules/@ibm/plex-mono/fonts/complete/woff2/${filename}`, `${destination}/fonts/${filename}`);
}
const notices = [];
for (const [path, info] of Object.entries(lock.packages)) {
  if (!path || info.dev || info.optional) continue;
  for (const filename of await readdir(path)) {
    if (/^(licen[sc]e|copying|notice)(\.|$)/i.test(filename)) {
      const license = (await readFile(`${path}/${filename}`, 'utf8'))
        .replace(/\r\n/g, '\n').replace(/[ \t]+$/gm, '');
      notices.push(`${path} ${info.version}\n${license}`);
    }
  }
}
await writeFile(`${destination}/LICENSES.txt`, notices.join('\n\n---\n\n'));
