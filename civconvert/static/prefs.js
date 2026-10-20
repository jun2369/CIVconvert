/* Runs in <head> before the page is drawn: the saved light / dark mode and language (no flash). */
(() => {
  try {
    const theme = localStorage.getItem("civconvert.theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    document.documentElement.lang = localStorage.getItem("civconvert.lang") === "zh" ? "zh-CN" : "en";
  } catch { /* storage blocked: system theme, English */ }
})();
