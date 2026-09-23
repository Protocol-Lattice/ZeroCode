import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

// The pinned compiler already lowers explicit references through the C ABI.
// Admit only our opaque session pointer at the header-import boundary; the
// native host never inspects or owns the Zero App layout.
const path = process.argv[2];
const original = '  char normalized[128];\n  if (!normalized_c_type(c_type, normalized, sizeof(normalized))) return false;';
const configured = `  /* ZeroCode live-session bridge: borrowed for one synchronous call. */
  if (strcmp(c_type, "struct ZeroLiveApp *") == 0) {
    snprintf(out, out_len, "mutref<App>");
    return true;
  }
${original}`;
const source = readFileSync(path, 'utf8');
if (!source.includes(configured)) {
  if (source.split(original).length !== 2) {
    throw new Error('Pinned Zero C import mapping changed; review the live-session bridge.');
  }
  writeFileSync(path, source.replace(original, configured));
}

// Normal Zero reference parameters already lower to a pointer-sized scalar.
// Apply that same representation only to this imported opaque reference, without
// changing the normal shape-reference lowering or admitting raw pointer casts.
for (const name of ['program_graph_mir.c', 'ir.c']) {
  const target = join(dirname(path), name);
  const originalText = readFileSync(target, 'utf8');
  let text = originalText;
  if (!text.includes('/* ZeroCode imported App reference ABI. */')) {
    let changed = 0;
    text = text.replace(/ir_type_kind\((function(?:->|\.)params\[[ip]\]\.zero_type)\)/g, (_, value) => {
      changed++;
      return `(strcmp(${value}, "mutref<App>") == 0 ? IR_TYPE_USIZE : ir_type_kind(${value}))`;
    });
    if (changed !== 4) throw new Error(`Pinned Zero external parameter lowering changed in ${name}: ${changed}`);
    text = '/* ZeroCode imported App reference ABI. */\n' + text;
  }
  // Complete system prompts and evolved tool descriptions share the package's
  // literal pool. Native emitters size their data sections from these segments.
  const limit = name === 'ir.c' ? 'IR_READONLY_DATA_LIMIT 65536u' : 'IR_READONLY_DATA_LIMIT = 65536u';
  const expanded = limit.replace('65536u', '1048576u');
  if (!text.includes(expanded)) {
    if (text.split(limit).length !== 2) throw new Error(`Pinned Zero literal limit changed in ${name}`);
    text = text.replace(limit, expanded);
  }
  if (text !== originalText) writeFileSync(target, text);
}
