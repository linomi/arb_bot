/**
 * i18n loader (en / fa). Dictionaries in /static/js/i18n/{lang}.json
 */
(function (global) {
  const STORAGE_KEY = "arb_lang";
  let STRINGS = { en: {}, fa: {} };
  let ready = false;
  const waiters = [];

  function getLang() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "fa" || saved === "en") return saved;
    return "en";
  }

  function t(key, vars) {
    const lang = getLang();
    let s = (STRINGS[lang] && STRINGS[lang][key]) || (STRINGS.en && STRINGS.en[key]) || key;
    if (vars) {
      Object.keys(vars).forEach((k) => {
        s = s.replace(new RegExp("\\{" + k + "\\}", "g"), String(vars[k]));
      });
    }
    return s;
  }

  function applyStatic() {
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (!key) return;
      const attr = el.getAttribute("data-i18n-attr");
      if (attr) el.setAttribute(attr, t(key));
      else if (el.getAttribute("data-i18n-html") === "1") el.innerHTML = t(key);
      else el.textContent = t(key);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
    const title = document.querySelector("title");
    if (title) title.textContent = t("app.title");
  }

  function setDir(lang) {
    const html = document.documentElement;
    if (lang === "fa") {
      html.setAttribute("lang", "fa");
      html.setAttribute("dir", "rtl");
      document.body.classList.add("lang-fa");
      document.body.classList.remove("lang-en");
    } else {
      html.setAttribute("lang", "en");
      html.setAttribute("dir", "ltr");
      document.body.classList.add("lang-en");
      document.body.classList.remove("lang-fa");
    }
  }

  function setLang(lang, opts) {
    if (lang !== "fa" && lang !== "en") lang = "en";
    localStorage.setItem(STORAGE_KEY, lang);
    setDir(lang);
    applyStatic();
    document.querySelectorAll("[data-lang-btn]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-lang-btn") === lang);
    });
    if (!opts || !opts.silent) {
      if (typeof global.__onLanguageChange === "function") global.__onLanguageChange(lang);
    }
  }

  async function loadDicts() {
    const [en, fa] = await Promise.all([
      fetch("/static/js/i18n/en.json").then((r) => r.json()),
      fetch("/static/js/i18n/fa.json").then((r) => r.json()),
    ]);
    STRINGS.en = en;
    STRINGS.fa = fa;
    ready = true;
    waiters.splice(0).forEach((fn) => fn());
  }

  function whenReady(fn) {
    if (ready) fn();
    else waiters.push(fn);
  }

  function init() {
    const lang = getLang();
    setDir(lang);
    document.querySelectorAll("[data-lang-btn]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-lang-btn") === lang);
      btn.addEventListener("click", () => setLang(btn.getAttribute("data-lang-btn")));
    });
    loadDicts()
      .then(() => {
        applyStatic();
      })
      .catch((e) => console.error("i18n load failed", e));
  }

  global.i18n = { t, setLang, getLang, applyStatic, init, whenReady };
  global.t = t;

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})(window);
