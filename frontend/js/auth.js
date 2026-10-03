/**
 * Simple login overlay. Shown when auth is required and the session is missing/expired.
 * Session cookie lasts 5 days (set by the server).
 */
(function () {
  const OVERLAY_ID = "auth-overlay";

  function ensureOverlay() {
    let el = document.getElementById(OVERLAY_ID);
    if (el) return el;

    el = document.createElement("div");
    el.id = OVERLAY_ID;
    el.innerHTML = `
      <div class="auth-card">
        <div class="auth-brand">
          <span class="brand-mark">&#8862;</span>
          <span>${typeof t === "function" ? t("brand.name") : "STAT-ARB TERMINAL"}</span>
        </div>
        <h2>${typeof t === "function" ? t("auth.signin") : "Sign in"}</h2>
        <p class="auth-hint">${typeof t === "function" ? t("auth.hint") : "Session stays valid for 5 days."}</p>
        <form id="auth-form">
          <label>${typeof t === "function" ? t("auth.username") : "Username"}
            <input type="text" name="username" autocomplete="username" required autofocus />
          </label>
          <label>${typeof t === "function" ? t("auth.password") : "Password"}
            <input type="password" name="password" autocomplete="current-password" required />
          </label>
          <div class="auth-error" id="auth-error" hidden></div>
          <button type="submit" class="btn-primary auth-submit">${typeof t === "function" ? t("auth.submit") : "Sign in"}</button>
        </form>
      </div>
    `;
    document.body.appendChild(el);

    el.querySelector("#auth-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.target;
      const username = form.username.value.trim();
      const password = form.password.value;
      const errEl = document.getElementById("auth-error");
      errEl.hidden = true;
      try {
        await API.login(username, password);
        hideLogin();
        if (typeof window.__onAuthSuccess === "function") {
          window.__onAuthSuccess();
        } else {
          location.reload();
        }
      } catch (err) {
        errEl.textContent = err.message || (typeof t === "function" ? t("auth.failed") : "Login failed");
        errEl.hidden = false;
      }
    });

    return el;
  }

  function showLogin() {
    const el = ensureOverlay();
    el.classList.add("visible");
    document.body.classList.add("auth-locked");
    const input = el.querySelector('input[name="username"]');
    if (input) input.focus();
  }

  function hideLogin() {
    const el = document.getElementById(OVERLAY_ID);
    if (el) el.classList.remove("visible");
    document.body.classList.remove("auth-locked");
  }

  window.__onAuthRequired = showLogin;
  window.__showLogin = showLogin;
  window.__hideLogin = hideLogin;

  async function bootstrapAuth() {
    try {
      const s = await API.authStatus();
      if (s.auth_required && !s.authenticated) {
        showLogin();
      }
    } catch (e) {
      console.warn("auth status check", e);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrapAuth);
  } else {
    bootstrapAuth();
  }
})();
