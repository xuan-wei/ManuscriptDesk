(() => {
  const announce = (message, type = '') => {
    const live = document.querySelector('.account-live');
    if (!live) return;
    live.className = `account-live account-inline-status${type ? ` is-${type}` : ''}`;
    live.textContent = message;
  };

  document.querySelectorAll('[data-password-toggle]').forEach((button) => {
    const input = document.getElementById(button.getAttribute('aria-controls'));
    if (!input) return;
    button.addEventListener('click', () => {
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      button.setAttribute('aria-pressed', String(!showing));
      button.textContent = showing ? '显示密码' : '隐藏密码';
    });
  });

  document.querySelectorAll('[data-step-next]').forEach((button) => {
    button.addEventListener('click', async () => {
      const current = button.closest('[data-account-step]');
      const next = document.querySelector(`[data-account-step="${button.dataset.stepNext}"]`);
      if (!current || !next) return;
      const required = [...current.querySelectorAll('[required]')];
      const invalid = required.find((field) => !field.reportValidity());
      if (invalid) return;
      if (button.dataset.sendCode) {
        const form = button.closest('form');
        const email = form?.elements.namedItem('email')?.value || '';
        const captchaToken = form?.elements.namedItem('captcha_token')?.value || '';
        const captchaAnswer = form?.elements.namedItem('captcha_answer')?.value || '';
        const originalLabel = button.textContent;
        button.disabled = true;
        button.textContent = '正在发送…';
        try {
          const response = await fetch(button.dataset.sendCode, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': form?.elements.namedItem('csrf_token')?.value || '' },
            body: JSON.stringify({ email, captcha_token: captchaToken, captcha_answer: captchaAnswer })
          });
          if (!response.ok) throw new Error('验证码发送失败，请检查图片验证码后重试。');
        } catch (error) {
          announce(error.message || '验证码发送失败，请稍后重试。', 'error');
          button.disabled = false;
          button.textContent = originalLabel;
          return;
        }
        button.disabled = false;
        button.textContent = originalLabel;
      }
      current.hidden = true;
      next.hidden = false;
      document.querySelectorAll('.account-stepper li').forEach((item) => item.classList.toggle('is-current', item.dataset.stepId === button.dataset.stepNext));
      (next.querySelector('input,button') || next).focus();
      announce('验证码已发送，请查看邮箱。', 'success');
    });
  });

  document.querySelectorAll('[data-step-back]').forEach((button) => {
    button.addEventListener('click', () => {
      const current = button.closest('[data-account-step]');
      const previous = document.querySelector(`[data-account-step="${button.dataset.stepBack}"]`);
      if (!current || !previous) return;
      current.hidden = true;
      previous.hidden = false;
      document.querySelectorAll('.account-stepper li').forEach((item) => item.classList.toggle('is-current', item.dataset.stepId === button.dataset.stepBack));
      (previous.querySelector('input,button') || previous).focus();
    });
  });

  document.querySelectorAll('[data-dialog-open]').forEach((button) => {
    button.addEventListener('click', () => document.getElementById(button.dataset.dialogOpen)?.showModal());
  });
  document.querySelectorAll('[data-dialog-close]').forEach((button) => {
    button.addEventListener('click', () => button.closest('dialog')?.close());
  });
  document.querySelectorAll('dialog.account-dialog').forEach((dialog) => {
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) dialog.close();
    });
  });

  document.querySelectorAll('[data-copy-target]').forEach((button) => {
    button.addEventListener('click', async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      try {
        await navigator.clipboard.writeText(target.textContent.trim());
        announce('已复制。', 'success');
        button.textContent = '已复制';
      } catch (_) {
        announce('无法自动复制，请手动选择文本。', 'error');
        target.focus?.();
      }
    });
  });

  document.querySelectorAll('[data-submit-once]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (form.dataset.submitted === 'true') {
        event.preventDefault();
        return;
      }
      if (!form.checkValidity()) return;
      form.dataset.submitted = 'true';
      form.setAttribute('aria-busy', 'true');
      const button = form.querySelector('button[type="submit"]');
      if (button) {
        button.disabled = true;
        button.textContent = '正在发送…';
      }
    });
  });

  document.querySelectorAll('[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const rawTokenDialog = document.getElementById('raw-token-dialog');
  if (rawTokenDialog?.dataset.hasToken === 'true') {
    rawTokenDialog.showModal();
    rawTokenDialog.querySelector('[data-copy-target]')?.focus();
  }
})();
