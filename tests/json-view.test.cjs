const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

const context = vm.createContext({Blob});
vm.runInContext(fs.readFileSync('src/gusnotebook/static/js/json-view.js', 'utf8'), context);
const parse = source => context.parseJsonView(source);

function serialize(node) {
  const value = node.children
    ? node.raw + node.children.map(serialize).join(',') + (node.raw === '{' ? '}' : ']')
    : node.raw;
  return (node.key === null ? '' : node.key + ':') + value;
}

test('JSON viewer preserves numeric literals, duplicate keys, escapes and property order', () => {
  const source = '{"b":9007199254740993,"b":-0,"2":1.2300e+400,"a\\\"b":"\\\\ \\u0061 \\n", "empty":{}, "list":[[],null,true,false]}';
  assert.equal(serialize(parse(source)), source.replace(', "empty"', ',"empty"').replace(', "list"', ',"list"'));
});

test('JSON viewer accepts root scalars, empty containers and a UTF-8 BOM', () => {
  for (const source of ['null', 'true', 'false', '0', '-1.20e-10', '"hello"', '{}', '[]']) {
    assert.equal(serialize(parse('\uFEFF \n' + source + '\n')), source);
  }
});

test('JSON viewer rejects invalid documents rather than displaying a partial tree', () => {
  for (const source of ['', '{bad', '[1,]', '{"x":1} garbage', 'true false', '01', 'NaN']) {
    assert.throws(() => parse(source), /Invalid JSON/);
  }
});

test('JSON viewer falls back for excessive size, depth, token count or literal length', () => {
  assert.throws(() => parse('"' + 'é'.repeat(1024 * 1024) + '"'), /limited to 2 MB/);
  assert.throws(() => parse('['.repeat(65) + '0' + ']'.repeat(65)), /deeply nested/);
  assert.throws(() => parse(JSON.stringify(Array(6000).fill(0))), /too complex/);
  assert.throws(() => parse(JSON.stringify('x'.repeat(10001))), /too complex/);
});
