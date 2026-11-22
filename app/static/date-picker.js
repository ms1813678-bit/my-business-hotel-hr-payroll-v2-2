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

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
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
      /* Calendar + time-picker paths bypass parseDate: re-validate the
       * intended wall components (raw time-control values captured before
       * the browser can normalize them into a Date) and refuse to keep an
       * invented time. */
      config.onValueUpdate = function () {
        validatePopupTime();
      };
    }
    config.onClose = function () {
      if (!instance || !instance.selectedDates) {
        return;
      }
      /* Clicking another picker or outside can make flatpickr clear a field
       * that holds a preserved historical value (no selectedDates). The
       * stored original must survive until the user explicitly types a valid
       * correction, so restore it after flatpickr's own close handling. */
      if (preserveInitial && instance.selectedDates.length === 0 && lastText === initialDisplay) {
        el.value = initialISO;
        altInput.value = initialDisplay;
        clearInvalid();
        return;
      }
      if (withTime) {
        validatePopupTime();
        reassertRejected();
      }
    };
    if (el.getAttribute("min")) {
      config.minDate = el.getAttribute("min");
    }
    if (el.getAttribute("max")) {
      config.maxDate = el.getAttribute("max");
    }
    var instance;
    try {
      instance = window.flatpickr(el, config);
    } finally {
      /* config callbacks can fire during construction; instance is still
       * undefined then and every callback guards on it. */
    }
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

    /* A rejected popup attempt (nonexistent Cairo wall time): the attempt
     * must survive flatpickr's own close/blur clearing until the user makes
     * an explicit valid correction. */
    var rejectedDisplay = null;
    var rejectedDate = null;
    function reassertRejected() {
      if (!rejectedDisplay) {
        return;
      }
      if (!rejectedDate && !instance.selectedDates.length) {
        rejectedDisplay = null;
        return;
      }
      if (!instance.selectedDates.length || el.value === "") {
        el.value = "";
        altInput.value = rejectedDisplay;
        lastText = rejectedDisplay;
        markInvalid(error.textContent);
      }
    }

    /* Raw values the user entered into the popup time controls, captured in
     * the capture phase BEFORE flatpickr can write back a browser-normalized
     * value (a Cairo browser turns a nonexistent 00:30 into 01:30). Keyed by
     * control so a later flatpickr write-back is never mistaken for intent. */
    var userTime = {};
    function validatePopupTime() {
      if (!withTime || !instance || !instance.selectedDates) {
        return;
      }
      var d = instance.selectedDates[0];
      var year;
      var month;
      var day;
      if (rejectedDate && (userTime.hour !== undefined || userTime.minute !== undefined)) {
        /* A correction follows a rejection: it belongs to the rejected
         * attempt's date, even if the selection was cleared or the picker
         * re-anchored to today. */
        year = rejectedDate.year;
        month = rejectedDate.month;
        day = rejectedDate.day;
      } else if (d) {
        year = d.getFullYear();
        month = d.getMonth() + 1;
        day = d.getDate();
      } else {
        return;
      }
      var hour = userTime.hour !== undefined ? userTime.hour : d.getHours();
      var minute = userTime.minute !== undefined ? userTime.minute : d.getMinutes();
      if (!validTime(hour, minute)) {
        return;
      }
      var iso = year + "-" + pad2(month) + "-" + pad2(day) + "T" + pad2(hour) + ":" + pad2(minute);
      if (!cairoWallExists(year, month, day, hour, minute)) {
        /* Intended Cairo wall time does not exist (skipped DST hour): keep
         * the attempt visible, submit nothing, and block the form even when
         * the field is optional. Never keep the browser-normalized time. */
        var display = isoToDisplay(iso, true);
        rejectedDisplay = display;
        rejectedDate = { year: year, month: month, day: day };
        el.value = "";
        altInput.value = display;
        lastText = display;
        markInvalid(error.textContent);
        return;
      }
      /* Valid: rebuild the submitted ISO from the intended components so a
       * normalized Date can never leak into the submission. */
      rejectedDisplay = null;
      rejectedDate = null;
      userTime = {};
      el.value = iso;
      var acceptedDisplay = isoToDisplay(iso, true);
      altInput.value = acceptedDisplay;
      lastText = acceptedDisplay;
      clearInvalid();
    }
    if (withTime && instance.timeContainer) {
      instance.timeContainer.addEventListener("input", function (ev) {
        if (ev.target === instance.hourElement) {
          userTime.hour = Number(instance.hourElement.value);
        } else if (ev.target === instance.minuteElement) {
          userTime.minute = Number(instance.minuteElement.value);
        }
      }, true);
      instance.timeContainer.addEventListener("increment", function (ev) {
        if (ev.target === instance.hourElement) {
          userTime.hour = Number(instance.hourElement.value);
        } else if (ev.target === instance.minuteElement) {
          userTime.minute = Number(instance.minuteElement.value);
        }
      }, true);
      /* A fresh day click resets the intended time to the picker default. */
      if (instance.calendarContainer) {
        instance.calendarContainer.addEventListener("click", function (ev) {
          if (ev.target && ev.target.classList && ev.target.classList.contains("flatpickr-day")) {
            userTime = {};
          }
        }, true);
      }
    }
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
      if (altInput.value.trim() !== (rejectedDisplay || initialDisplay || "")) {
        /* The user is typing their own text: every stale popup intent
         * (raw time-control values, rejected attempt and its date) belongs
         * to the abandoned popup interaction and must not shape this one. */
        rejectedDisplay = null;
        rejectedDate = null;
        userTime = {};
      }
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
      /* Runs after flatpickr's own blur handling (registered at creation).
       * Note: focusing the popup time controls also blurs the visible input,
       * so this must not clear a rejection while the attempt is visible. */
      var visible = altInput.value.trim() !== "" ? altInput.value : lastText;
      if (visible.trim() === "") {
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
        /* Valid text (typed or produced by a valid popup selection): adopt
         * it through the normal path so the hidden ISO value is set. An
         * explicit typed correction abandons any stale popup intent first,
         * otherwise the nested onValueUpdate would re-reject using raw time
         * values captured for the old attempt. */
        if (visible !== (rejectedDisplay || "") || rejectedDisplay === null) {
          userTime = {};
          rejectedDate = null;
          rejectedDisplay = null;
        }
        instance.setDate(parsed, true);
        if (!withTime || el.value !== "") {
          clearInvalid();
        }
      } else {
      /* Invalid or nonexistent Cairo wall time: the attempt stays visible,
       * nothing submits, and the custom validity blocks the form even when
       * the field is optional. */
      altInput.value = visible;
      el.value = "";
      markInvalid(error.textContent);
      }
      /* flatpickr's own blur handling is debounced and can still clear the
       * attempt after this handler; re-assert it once that has settled. */
      window.setTimeout(reassertRejected, 400);
    });
  }

  document.querySelectorAll('input[type="date"]').forEach(function (el) {
    enhance(el, false);
  });
  document.querySelectorAll('input[type="datetime-local"]').forEach(function (el) {
    enhance(el, true);
  });
})();
