// 横幅の実測補正: 右パネル分の混入、中央揃えの左はみ出し、最小幅を検証する。
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../../static/solitaire/js/main.js'), 'utf8');
const start = source.indexOf('function fitBoardWidthByMeasurement(');
const end = source.indexOf('\n/**', start);
assert(start >= 0 && end > start);
function fixture(fieldWidth, initial, minimum, sides = true) {
  let width = initial;
  let probes = 0;
  const style = {display:'block', marginLeft:'0px', marginRight:'0px', paddingLeft:'0px', paddingRight:'0px', columnGap:'10px'};
  const grid = {getBoundingClientRect:()=>({width:6*width+50}), get scrollWidth(){return 6*width+52;}};
  const side = {getBoundingClientRect:()=>({width:2.4*width+46}), get scrollWidth(){return Math.round(2.4*width+46);}, hidden:!sides};
  const content = {children:[grid,side]};
  const field = {clientWidth:fieldWidth};
  const board = {querySelector:()=>content};
  const sandbox = {
    document:{querySelector:()=>field,getElementById:()=>board},
    getComputedStyle(el) {
      if(el===field) return {...style,paddingLeft:'10px',paddingRight:'6px'};
      if(el===board) return {...style,paddingLeft:(width+40)+'px',paddingRight:'6px'};
      return {...style,display:el.hidden?'none':'block'};
    },
    applySlotWidth(w){width=w;probes++;},
  };
  vm.createContext(sandbox); vm.runInContext(source.slice(start,end),sandbox);
  const result=sandbox.fitBoardWidthByMeasurement(initial,minimum);
  assert.equal(width,result);
  assert(probes <= 9, '探索回数はカード幅の範囲に対して対数的');
  return result;
}
// 実画面1280px、左右パネル各180px: 盤面列920px。83pxでははみ出し、79pxなら収まる。
assert.equal(fixture(920,83,60),79);
// 十分な横幅では高さ由来のカード幅を変更しない。
assert.equal(fixture(1560,110,60),110);
// 最小幅でも足りない画面は操作サイズを維持し、スクロールを許す。
assert.equal(fixture(500,90,60),60);
assert.equal(fixture(500,60,60),60);
// 非表示領域の幅と間隔は計上しない。
assert.equal(fixture(920,83,60,false),83);
console.log('盤面幅: 実測寸法・境界値・非表示領域の5ケース成功');
