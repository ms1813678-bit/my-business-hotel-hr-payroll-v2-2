/* Item 47: show the chosen file name next to the localized picker button.
 * All visible interface text lives in the HTML (translate layer localizes
 * it); this script only copies the selected file's own name, which is user
 * data and is never translated. */
(function () {
  "use strict";
  document.querySelectorAll(".file-picker").forEach(function (picker) {
    var input = picker.querySelector(".file-picker-input");
    var name = picker.querySelector(".file-picker-name");
    if (!input || !name) {
      return;
    }
    var emptyText = name.textContent;
    input.addEventListener("change", function () {
      name.textContent = input.files && input.files.length ? input.files[0].name : emptyText;
    });
  });
})();
