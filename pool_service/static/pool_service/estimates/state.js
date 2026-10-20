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
