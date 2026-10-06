// Probe app.asar for "provider_config" occurrences (streaming, low memory).
const fs = require('fs');
const st = fs.statSync('D:/zcode-3.11.2/resources/app.asar');
const fd = fs.openSync('D:/zcode-3.11.2/resources/app.asar', 'r');
const CH = 4 * 1024 * 1024;
const buf = Buffer.alloc(CH);
let off = 0, hits = 0;
const out = [];
while (off < st.size && hits < 3) {
  const n = fs.readSync(fd, buf, 0, CH, off);
  if (n <= 0) break;
  const s = buf.toString('latin1', 0, n);
  let i = 0;
  while ((i = s.indexOf('provider_config', i)) >= 0) {
    out.push('hit at file offset ' + (off + i));
    out.push(s.slice(Math.max(0, i - 200), i + 300).replace(/[^\x20-\x7e]/g, '.'));
    hits++;
    if (hits >= 3) break;
    i += 15;
  }
  off += n - 15;
}
out.push('done hits=' + hits);
fs.writeFileSync('C:/Users/qazxc/AppData/Local/Temp/asar_probe4.txt', out.join('\n'));
console.log('DONE hits=' + hits);
