// Stock evidence and permission to place an order are separate facts.
export function availabilityStatus(product) {
  const facts = product.availability || {};
  let tone = 'pending', label = 'Уточнить наличие';
  if (facts.issue) {
    // Contradictory evidence must never look like confirmed stock.
  } else if (facts.state === 'unavailable' || facts.orderable === false) {
    tone = 'unavailable'; label = 'Нет в наличии';
  } else if (product.available === true && facts.orderable === true && ['own_stock', 'supplier_stock'].includes(facts.state)) {
    tone = 'confirmed'; label = facts.state === 'own_stock' ? 'У нас' : 'У поставщика';
  } else if (facts.state === 'preorder') {
    label = 'Под заказ';
  }
  const detail = [facts.label, facts.issue].filter(Boolean).join(' · ');
  return {tone, label, detail: detail ? `${label}: ${detail}` : label};
}
