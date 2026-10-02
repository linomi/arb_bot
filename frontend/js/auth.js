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
          <span>STAT-ARB TERMINAL</span>
        </div>
        <h2>Sign in</h2>
        <p class="auth-hint">Session stays valid for 5 days.</p>
        <form id="auth-form">
          <label>Username
            <input type="text" name="username" autocomplete="username" required autofocus />
          </label>
          <label>Password
            <input type="password" name="password" autocomplete="current-password" required />
          </label>
          <div class="auth-error" id="auth-error" hidden></div>
          <button type="submit" class="btn-primary auth-submit">Sign in</button>
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
        // Reload app state after successful login
        if (typeof window.__onAuthSuccess === "function") {
          window.__onAuthSuccess();
        } else {
          location.reload();
        }
      } catch (err) {
        errEl.textContent = err.message || "Login failed";
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

  // On load: check session status
  async function bootstrapAuth() {
    try {
      const s = await API.authStatus();
      if (s.auth_required && !s.authenticated) {
        showLogin();
      }
    } catch (e) {
      // Network or other error — if we got 401 the handler already showed login
      console.warn("auth status check", e);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrapAuth);
  } else {
    bootstrapAuth();
  }
})();
