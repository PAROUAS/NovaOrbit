(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.NOVACategorySelection = factory();
}(globalThis, function () {
  return function selectCategory(category, query, search, select) {
    const term = String(query || '').trim();
    if (term) search(term, category);
    else select(category);
  };
}));
