// Seal confirmation is usable without JavaScript (native `required` checkbox
// plus server-side binding checks). This only enables/disables the submit
// button so the explicit-confirmation state is obvious before clicking.
document.querySelectorAll('[data-seal-form]').forEach((form) => {
  const checkbox = form.querySelector('[data-confirm-check]');
  const button = form.querySelector('[data-seal-button]');
  if (!checkbox || !button) return;
  const sync = () => {
    button.disabled = !checkbox.checked;
  };
  checkbox.addEventListener('change', sync);
  sync();
});
