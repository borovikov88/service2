// Presentation only: never serialize the rounded value in place of the source.
export const indivisible = unit => /^(шт\.?|компл\.?|комплект(?:а|ов)?|упак\.?|пара|пар)$/iu.test(String(unit).trim());
export function exactNumber(value) {
  if (value === null || value === undefined || value === '') return '';
  const text = String(value).replace(',', '.');
  if (!Number.isFinite(Number(text))) return text;
  return text.replace(/^(-?)\./, '$10.').replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1');
}
export function displayNumber(value, {exact=false, whole=false}={}) {
  const raw = exactNumber(value);
  if (raw === '' || !Number.isFinite(Number(raw))) return raw;
  // Historical fractional pieces must remain visible, not silently become whole.
  if (exact || (whole && !Number.isInteger(Number(raw)))) return raw;
  return new Intl.NumberFormat('en-US', {useGrouping:false, maximumFractionDigits:whole ? 0 : 1}).format(Number(raw));
}
export const money = value => value === null || value === undefined ? '—' : displayNumber(value).replace('.', ',');
export const cleanNote = text => String(text || '').replace(/(?<![\w/])\d+\.\d+/g, value => exactNumber(value));
const states = new WeakMap();
export function rawValue(el) {
  const state = states.get(el);
  return state && (!state.edited || el.value === state.display) ? state.raw : el.value;
}
export function bindNumber(el, value, options={}) {
  const raw = value == null ? '' : String(value).replace(',', '.');
  const display = displayNumber(raw, options);
  states.set(el, {raw, display, options, edited:false});
  el.value = display;
  el.setCustomValidity('');
  el.inputMode = 'decimal';
  el.title = raw ? `Точное значение: ${exactNumber(raw)}. При редактировании показано полностью.` : '';
}
export function installNumbers(root) {
  root.addEventListener('focusin', ({target:el}) => {
    const s=states.get(el); if(s) { el.value=exactNumber(s.raw); el.select?.(); }
  });
  root.addEventListener('input', ({target:el}) => {
    const s=states.get(el); if(!s)return;
    s.raw=el.value; s.edited=true;
    el.setCustomValidity(s.options.whole && el.value!=='' && !Number.isInteger(Number(el.value)) ? 'Укажите целое количество для штучной позиции.' : '');
  }, true);
  root.addEventListener('change', event => {
    const s=states.get(event.target);
    if(s && !s.edited) event.stopImmediatePropagation();
  }, true);
  root.addEventListener('focusout', ({target:el}) => {
    const s=states.get(el); if(!s)return;
    // Keep invalid raw input visible; rounding can hide a step/min violation.
    if(el.validity?.valid === false)return;
    s.display=displayNumber(s.raw,s.options); el.value=s.display;
    el.title=s.raw ? `Точное значение: ${exactNumber(s.raw)}. При редактировании показано полностью.` : '';
  });
  root.addEventListener('keydown', event => {
    if(event.key !== ',' || event.target.type !== 'number')return;
    event.preventDefault();
    // Browser editing preserves the caret (number inputs have no selectionStart).
    event.target.ownerDocument.execCommand('insertText', false, '.');
  });
  // Number inputs stay locale-safe; normalize a pasted decimal comma explicitly.
  root.addEventListener('paste', event => {
    const el=event.target; if(el.type !== 'number')return;
    const text=event.clipboardData?.getData('text').trim();
    if(!/^\d*,\d+$/.test(text || ''))return;
    event.preventDefault();
    el.ownerDocument.execCommand('insertText', false, text.replace(',','.'));
  });
}
