/* Unified day/month/year date picker.
 *
 * Native <input type="date"> widgets render in the browser's own locale
 * (often mm/dd/yyyy), which confuses a day/month/year operation. flatpickr
 * keeps the submitted value ISO (Y-m-d), so server parsing is unchanged,
 * while the visible field always shows d/m/Y in the current UI language.
 */
(function () {
  "use strict";
  if (!window.flatpickr) {
    return;
  }
  /* The translate layer localizes <html lang>: ar, en, or zh-CN. */
  var lang = (document.documentElement.getAttribute("lang") || "ar").slice(0, 2);
  var locale = "default";
  if (lang === "ar" && window.flatpickr.l10ns && window.flatpickr.l10ns.ar) {
    locale = window.flatpickr.l10ns.ar;
  } else if (lang === "zh" && window.flatpickr.l10ns && window.flatpickr.l10ns.zh) {
    locale = window.flatpickr.l10ns.zh;
  }
  document.querySelectorAll('input[type="date"]').forEach(function (el) {
    var config = {
      dateFormat: "Y-m-d",
      altInput: true,
      altFormat: "d/m/Y",
      allowInput: true,
      locale: locale
    };
    if (el.getAttribute("min")) {
      config.minDate = el.getAttribute("min");
    }
    if (el.getAttribute("max")) {
      config.maxDate = el.getAttribute("max");
    }
    window.flatpickr(el, config);
  });
})();
