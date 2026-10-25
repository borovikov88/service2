export function setExcluded(snapshot, variantKey, blockId, rowId, excluded, all = false) {
  const source = rowId ? snapshot.variants[variantKey].blocks.find(b => b.id === blockId)?.rows.find(r => r.id === rowId) : null;
  for (const [key, variant] of Object.entries(snapshot.variants)) {
    if (!all && key !== variantKey) continue;
    const block = variant.blocks.find(b => b.id === blockId);
    if (!block) continue;
    const target = rowId ? (source?.embedded_role && key !== variantKey
      ? block.rows.find(r => r.embedded_role === source.embedded_role && r.embedded_profile === variant.embedded_profile)
      : block.rows.find(r => r.id === rowId)) : block;
    if (target) {
      target.excluded = excluded;
      target.reason = excluded ? 'Исключено пользователем' : '';
      if(rowId && ['thermostat','filter_media','filter_gravel','filter_valve'].includes(target.kind))target.selection_locked=true;
    }
    // Restoring a row inside an excluded block restores only that row.
    if (rowId && target && !excluded && block.excluded) {
      for (const row of block.rows) {
        if (row.id !== target.id && !row.excluded) {
          row.excluded = true;
          row.reason = block.reason || 'Исключено вместе с блоком';
        }
      }
      block.excluded = false;
      block.reason = '';
    }
  }
  return snapshot;
}

export function emptyRow(id) {
  return {id, name: '', unit: 'шт.', kind: 'other', brand: '', model: '', quantity: '1', price: null,
    excluded: false, reason: '', existing: false, basis: '', auto_quantity: false,
    specs: {flow: null, head: null, filter_flow: null, backwash_flow: null}};
}

// Check all destinations before mutating; clones never share nested row data.
export function addRow(snapshot,variantKey,blockId,row,all=false){
 const result=structuredClone(snapshot);
 const targets=Object.entries(result.variants).filter(([key])=>all||key===variantKey);
 if(!targets.length)throw Error('Выберите смету.');
 for(const [,variant] of targets){
  const block=variant.blocks.find(b=>b.id===blockId);
  if(!block||block.rows.length>=100)throw Error('В одном из выбранных блоков нет места для новой строки (максимум 100).');
  if(variant.blocks.some(b=>b.rows.some(r=>r.id===row.id)))throw Error('Позиция уже добавлена.');
 }
 for(const [key,variant] of targets){
  variant.blocks.find(b=>b.id===blockId).rows.push(structuredClone(row));
  setExcluded(result,key,blockId,row.id,false);
 }
 return result;
}

export function isWork(row) {
  return row.kind === 'work' || ['bowl_work', 'embedded_work', 'equipment_work'].includes(row.id) || row.id.startsWith('finish_work');
}
export function samePrice(a, b) {
  return (a === null || a === '') && (b === null || b === '') ||
    a !== null && a !== '' && b !== null && b !== '' && Number(a) === Number(b);
}
export function workPriceTargets(snapshot, variantKey, blockId, source) {
  if (!isWork(source)) return [];
  return Object.entries(snapshot.variants).flatMap(([key, variant]) => {
    const rows = variant.blocks.find(b => b.id === blockId)?.rows || [];
    const matches = rows.filter(r => isWork(r) && r.unit === source.unit && (r.finish_type || '') === (source.finish_type || '') &&
      (source.work_code ? r.work_code === source.work_code : r.id === source.id || r.name === source.name));
    const exact = matches.find(r => r.id === source.id);
    const row = exact || (matches.length === 1 ? matches[0] : null);
    return row ? [{variant: key, row}] : [];
  });
}
export function setWorkPrice(snapshot, variantKey, blockId, source, price, all = false) {
  for (const target of workPriceTargets(snapshot, variantKey, blockId, source)) {
    if (all || target.variant === variantKey) target.row.price = price;
  }
  return snapshot;
}

export function automaticQuantity(snapshot, variantKey, row) {
  if(row.basis==='contact_work')return snapshot.variants[variantKey].blocks.flatMap(b=>b.rows).find(r=>r.work_code==='plaster')?.quantity??null;
  if (['filter_load', 'filter_valve'].includes(row.basis)) return row.quantity; // Server uses saved filter/accessory facts.
  if (row.basis?.startsWith('pkg_')) return row.quantity; // Server owns exact construction quantities and handling scope.
  if (row.basis === 'liner_corner') return String(Math.ceil(Number(snapshot.geometry.perimeter)/2));
  if (row.basis === 'lighting') return ['light_transformer','light_controller'].includes(row.kind) ? '1' : snapshot.inputs.light_count;
  if (row.basis === 'level_system') return '1';
  const variant = snapshot.variants[variantKey];
  const value = row.basis === 'exchanger_quantity'
    ? variant.blocks.flatMap(b => b.rows).find(r => r.id === row.heating_parent)?.quantity
    : variant.embedded_counts?.[row.basis] ?? snapshot.geometry[row.basis];
  return value === null || value === undefined || value === '' || !Number.isFinite(Number(value))
    ? null : String(Math.ceil(Number(value)));
}

export function quantityRequired(row) {
  return !isWork(row) && !(row.basis && row.auto_quantity);
}

// Reorder visible rows only within this table/quote. Hidden slots stay in place.
export function moveRow(snapshot, variantKey, blockId, rowId, direction, visibleIds=null) {
  if (![1, -1].includes(direction)) return false;
  const block = snapshot.variants[variantKey]?.blocks.find(b => b.id === blockId);
  if (!block || block.excluded) return false;
  const visible = block.rows.map((row, index) => ({row, index})).filter(({row}) => !row.excluded && (!visibleIds || visibleIds.has(row.id)));
  const from = visible.findIndex(({row}) => row.id === rowId), to = from + direction;
  if (from < 0 || to < 0 || to >= visible.length) return false;
  const a = visible[from].index, b = visible[to].index;
  [block.rows[a], block.rows[b]] = [block.rows[b], block.rows[a]];
  return true;
}

export function constructionScope(variant){
 return variant.blocks.filter(b=>['construction','construction_materials'].includes(b.id));
}
export function setConstructionExcluded(snapshot,variantKey,excluded,all=false){
 for(const [key,variant] of Object.entries(snapshot.variants)){
  if(!all&&key!==variantKey)continue;
  for(const block of constructionScope(variant)){
   block.excluded=excluded;block.reason=excluded?'Исключено пользователем':'';
  }
 }
 return snapshot;
}
