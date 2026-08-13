// Optional self-check: render a .docx to a PNG so you can eyeball the boxes
// without Word/LibreOffice.
//
//   npm i docx-preview jszip puppeteer      # one-time, in this skill dir
//   node preview.js <file.docx> <out.png> [heightPx]
//
// Renders via docx-preview in headless Chromium (approximates Word; good enough
// to confirm shading/borders show up).
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
(async () => {
  const [,, docxPath, outPng, heightPx] = process.argv;
  const nm = path.join(__dirname, 'node_modules');
  const b = await puppeteer.launch({ args: ['--no-sandbox', '--disable-setuid-sandbox'] });
  const p = await b.newPage();
  await p.setViewport({ width: 1000, height: parseInt(heightPx || '1600', 10), deviceScaleFactor: 2 });
  await p.setContent('<!doctype html><html><body style="margin:0"><div id="c"></div></body></html>');
  await p.addScriptTag({ path: path.join(nm, 'jszip/dist/jszip.min.js') });
  await p.addScriptTag({ path: path.join(nm, 'docx-preview/dist/docx-preview.js') });
  const b64 = fs.readFileSync(docxPath).toString('base64');
  await p.evaluate(async (b64) => {
    const bin = atob(b64), arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    await window.docx.renderAsync(arr.buffer, document.getElementById('c'), null, { inWrapper: false });
  }, b64);
  await new Promise(r => setTimeout(r, 600));
  await p.screenshot({ path: outPng, clip: { x: 0, y: 0, width: 1000, height: parseInt(heightPx || '1600', 10) } });
  await b.close();
  console.log('preview:', outPng);
})();
