export function setExcluded(snapshot, variantKey, blockId, rowId, excluded, all = false) {
  for (const [key, variant] of Object.entries(snapshot.variants)) {
    if (!all && key !== variantKey) continue;
    const block = variant.blocks.find(b => b.id === blockId);
    if (!block) continue;
    const target = rowId ? block.rows.find(r => r.id === rowId) : block;
    if (target) {
      target.excluded = excluded;
      target.reason = excluded ? 'Исключено пользователем' : '';
    }
    // Restoring a row inside an excluded block restores only that row.
    if (rowId && target && !excluded && block.excluded) {
      for (const row of block.rows) {
        if (row.id !== rowId && !row.excluded) {
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
