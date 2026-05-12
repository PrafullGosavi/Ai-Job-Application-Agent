/**
 * Global JS for AI Job Agent.
 * Page-specific logic lives in <script> blocks within each template.
 */

// Auto-dismiss flash alerts after 4 seconds
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.alert-auto').forEach(el => {
    setTimeout(() => {
      el.style.transition = 'opacity .5s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 500);
    }, 4000);
  });
});
