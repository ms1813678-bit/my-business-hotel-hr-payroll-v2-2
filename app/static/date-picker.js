/* Unified day/month/year date (and date-time) picker.
 *
 * Native <input type="date"> / <input type="datetime-local"> widgets render
 * in the browser's own locale (often mm/dd/yyyy), which confuses a
 * day/month/year operation. flatpickr keeps the submitted value ISO
 * (Y-m-d, or Y-m-dTHH:MM for date-time), so server parsing is unchanged,
 * while the visible field always shows d/m/Y [HH:MM] in the current UI
 * language. Date-time values stay local wall clock, exactly as
 * datetime-local always meant them.
 *
 * Typed input is parsed strictly: flatpickr's default parser normalizes
 * overflow dates (31/02/2026 becomes 03/03/2026), which would silently
 * submit a date the user never meant. An impossible date is rejected: the
 * hidden ISO value is emptied so nothing unintended can submit, the typed
 * text stays visible, and a localized error is shown.
 *
 * The visible input keeps the original's id and aria bindings so explicit
 * <label for="..."> focus and assistive-technology naming keep working
 * (flatpickr turns the original into type="hidden").
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
  var ERROR_TEXT = {
    date: { ar: "تاريخ غير صالح", en: "Invalid date", zh: "日期无效" },
    datetime: { ar: "تاريخ أو وقت غير صالح", en: "Invalid date or time", zh: "日期或时间无效" }
  };

  function daysInMonth(year, month) {
    /* month is 1-based; Date day 0 is the last day of the previous month. */
    return new Date(year, month, 0).getDate();
  }

  function validParts(year, month, day) {
    return month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth(year, month);
  }

  function validTime(hour, minute) {
    return hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59;
  }

  /* The fields mean Africa/Cairo WALL CLOCK, not a browser-local instant.
   * A browser running in Cairo normalizes a nonexistent wall time when a
   * Date is constructed (2026-04-24 00:30 silently becomes 01:30), which
   * would submit a time the operator never typed. Existence is therefore
   * checked through Intl with an explicit Africa/Cairo zone, independent
   * of the browser timezone, for any year. */
  var CAIRO_FMT = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Africa/Cairo", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false
  });

  function cairoParts(instantMs) {
    var out = {};
    CAIRO_FMT.formatToParts(new Date(instantMs)).forEach(function (part) {
      if (part.type !== "literal") {
        out[part.type] = Number(part.value);
      }
    });
    if (out.hour === 24) {
      out.hour = 0;
    }
    return out;
  }

  function cairoOffsetMinutes(instantMs) {
    var p = cairoParts(instantMs);
    return (Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute) - instantMs) / 60000;
  }

  function cairoWallExists(year, month, day, hour, minute) {
    var wallUTC = Date.UTC(year, month - 1, day, hour, minute);
    var offset = cairoOffsetMinutes(wallUTC - cairoOffsetMinutes(wallUTC) * 60000);
    var instant = wallUTC - offset * 60000;
    var p = cairoParts(instant);
    return p.year === year && p.month === month && p.day === day &&
           p.hour === hour && p.minute === minute;
  }

  function componentsMatch(date, year, month, day, hour, minute) {
    /* Guard against browser-timezone normalization of the constructed Date:
     * the visible components must equal exactly what was parsed. */
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
      return false;
    }
    if (hour === undefined) {
      return true;
    }
    return date.getHours() === hour && date.getMinutes() === minute;
  }

  function strictParse(text, withTime) {
    /* Returns a local Date or null. Accepts only the exact display shape
     * (d/m/Y [H:MM]) or the submitted ISO shape (Y-m-d[THH:MM[:SS]]). */
    var value = String(text).trim();
    var match;
    if (value.indexOf("/") !== -1) {
      match = withTime
        ? /^(\d{1,2})\/(\d{1,2})\/(\d{4})[ T](\d{1,2}):(\d{2})$/.exec(value)
        : /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(value);
      if (!match) {
        return null;
      }
      var day = Number(match[1]);
      var month = Number(match[2]);
      var year = Number(match[3]);
      if (!validParts(year, month, day)) {
        return null;
      }
      if (!withTime) {
        var dateOnly = new Date(year, month - 1, day);
        return componentsMatch(dateOnly, year, month, day) ? dateOnly : null;
      }
      var hour = Number(match[4]);
      var minute = Number(match[5]);
      if (!validTime(hour, minute)) {
        return null;
      }
      if (!cairoWallExists(year, month, day, hour, minute)) {
        /* Nonexistent Cairo wall time (skipped DST hour): never normalize. */
        return null;
      }
      var parsed = new Date(year, month - 1, day, hour, minute);
      return componentsMatch(parsed, year, month, day, hour, minute) ? parsed : null;
    }
    match = withTime
      ? /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(:\d{2})?$/.exec(value)
      : /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!match) {
      return null;
    }
    var isoYear = Number(match[1]);
    var isoMonth = Number(match[2]);
    var isoDay = Number(match[3]);
    if (!validParts(isoYear, isoMonth, isoDay)) {
      return null;
    }
    if (!withTime) {
      var isoDateOnly = new Date(isoYear, isoMonth - 1, isoDay);
      return componentsMatch(isoDateOnly, isoYear, isoMonth, isoDay) ? isoDateOnly : null;
    }
    var isoHour = Number(match[4]);
    var isoMinute = Number(match[5]);
    if (!validTime(isoHour, isoMinute)) {
      return null;
    }
    if (!cairoWallExists(isoYear, isoMonth, isoDay, isoHour, isoMinute)) {
      return null;
    }
    var isoParsed = new Date(isoYear, isoMonth - 1, isoDay, isoHour, isoMinute);
    return componentsMatch(isoParsed, isoYear, isoMonth, isoDay, isoHour, isoMinute) ? isoParsed : null;
  }

  function isoToDisplay(iso, withTime) {
    /* Format an ISO wall value from its string components only, so a stored
     * value inside a skipped DST hour displays verbatim instead of being
     * normalized by a browser running in the Cairo timezone. */
    var match = withTime
      ? /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso).trim())
      : /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso).trim());
    if (!match) {
      return null;
    }
    var day = match[3];
    var month = match[2];
    var text = day + "/" + month + "/" + match[1];
    if (withTime) {
      text += " " + match[4] + ":" + match[5];
    }
    return text;
  }

  function enhance(el, withTime) {
    var initialISO = el.value || "";
    var config = {
      dateFormat: withTime ? "Y-m-d\\TH:i" : "Y-m-d",
      altInput: true,
      altFormat: withTime ? "d/m/Y H:i" : "d/m/Y",
      allowInput: true,
      locale: locale,
      parseDate: function (text) {
        var parsed = strictParse(text, withTime);
        return parsed === null ? undefined : parsed;
      }
    };
    if (withTime) {
      config.enableTime = true;
      config.time_24hr = true;
      /* Calendar selections start at midday so picking a day can never land
       * inside a skipped DST hour on its own. */
      config.defaultHour = 12;
      config.defaultMinute = 0;
      /* Calendar + time-picker paths bypass parseDate: re-validate the final
       * ISO wall time and refuse to keep an invented one. */
      config.onValueUpdate = function () {
        if (el.value && strictParse(el.value, true) === null && el.value !== initialISO) {
          el.value = "";
          markInvalid(error.textContent);
        }
      };
    }
    if (el.getAttribute("min")) {
      config.minDate = el.getAttribute("min");
    }
    if (el.getAttribute("max")) {
      config.maxDate = el.getAttribute("max");
    }
    var instance = window.flatpickr(el, config);
    var altInput = instance.altInput;
    if (!altInput) {
      return;
    }

    /* A preloaded value inside a skipped Cairo DST hour fails strict parsing
     * by design; it is a real stored value and must be shown and submitted
     * verbatim, never normalized and never silently dropped. */
    var initialDisplay = initialISO ? isoToDisplay(initialISO, withTime) : null;
    var preserveInitial = false;
    if (initialISO && initialDisplay && strictParse(initialISO, withTime) === null) {
      preserveInitial = true;
      el.value = initialISO;
      altInput.value = initialDisplay;
    }
    function isPreservedInitial(text) {
      return preserveInitial && String(text).trim() === initialDisplay;
    }

    /* Preserve explicit label/assistive bindings on the visible input. */
    if (el.id) {
      altInput.id = el.id;
      el.removeAttribute("id");
    }
    ["aria-label", "aria-labelledby", "aria-describedby"].forEach(function (name) {
      if (el.getAttribute(name)) {
        altInput.setAttribute(name, el.getAttribute(name));
      }
    });

    var error = document.createElement("span");
    error.className = "date-picker-error";
    error.style.display = "none";
    error.style.color = "#b3261e";
    error.style.fontSize = "0.85rem";
    error.setAttribute("role", "alert");
    var messages = ERROR_TEXT[withTime ? "datetime" : "date"];
    error.textContent = messages[lang] || messages.ar;
    altInput.insertAdjacentElement("afterend", error);

    /* flatpickr clears the visible field when strict parsing rejects the
     * typed text on blur; remember the last typed text so it can be kept
     * visible alongside the error instead of disappearing. */
    var lastText = preserveInitial ? initialDisplay : "";
    function markInvalid(message) {
      error.textContent = message;
      error.style.display = "";
      altInput.setAttribute("aria-invalid", "true");
      /* Native constraint validation blocks form submission even when the
       * field is optional and even if blur never fired (e.g. Enter). */
      altInput.setCustomValidity(message);
    }
    function clearInvalid() {
      error.style.display = "none";
      altInput.removeAttribute("aria-invalid");
      altInput.setCustomValidity("");
    }
    altInput.addEventListener("input", function () {
      lastText = altInput.value;
      var text = altInput.value.trim();
      if (text !== "" && strictParse(text, withTime) === null && !isPreservedInitial(text)) {
        markInvalid(error.textContent);
      } else {
        clearInvalid();
        if (isPreservedInitial(text)) {
          el.value = initialISO;
        }
      }
    });

    altInput.addEventListener("blur", function () {
      /* Runs after flatpickr's own blur handling (registered at creation). */
      var visible = altInput.value.trim() !== "" ? altInput.value : lastText;
      if (visible.trim() === "") {
        clearInvalid();
        return;
      }
      if (instance.selectedDates.length > 0) {
        clearInvalid();
        return;
      }
      if (isPreservedInitial(visible)) {
        altInput.value = visible;
        el.value = initialISO;
        clearInvalid();
        return;
      }
      var parsed = strictParse(visible, withTime);
      if (parsed !== null) {
        /* Valid text that flatpickr did not adopt (e.g. outside min/max is
         * handled by flatpickr itself); adopt it through the normal path. */
        instance.setDate(parsed, true);
        clearInvalid();
        return;
      }
      altInput.value = visible;
      el.value = "";
      markInvalid(error.textContent);
    });
  }

  document.querySelectorAll('input[type="date"]').forEach(function (el) {
    enhance(el, false);
  });
  document.querySelectorAll('input[type="datetime-local"]').forEach(function (el) {
    enhance(el, true);
  });
})();
