// Время на месте без DOM и без i18n (проверяется в node, web/tests): ключ i18n + параметры.
// Равные границы — одно число («на месте 105 мин»), а не «105–105 мин».
export function visitLabel(lo, hi) {
  if (hi == null || lo === hi) return { key: 'card.visitOne', vars: { min: lo } };
  return { key: 'card.visit', vars: { lo, hi } };
}
