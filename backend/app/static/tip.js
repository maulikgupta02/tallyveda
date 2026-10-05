// Metric tooltips: one floating box on <body>, so cards and scrolling tables
// can't clip it, placed below the label (above if there's no room) and kept
// inside the window.
(function () {
  var box = document.createElement("div");
  box.className = "tipbox";
  box.setAttribute("role", "tooltip");
  document.body.appendChild(box);
  var current = null;

  function show(el) {
    current = el;
    box.textContent = el.getAttribute("data-tip");
    box.style.visibility = "hidden";
    box.classList.add("on");
    var r = el.getBoundingClientRect(), b = box.getBoundingClientRect(), gap = 8, pad = 12;
    var top = r.bottom + gap;
    if (top + b.height > window.innerHeight - pad && r.top - gap - b.height > pad) top = r.top - gap - b.height;
    var left = Math.min(Math.max(pad, r.left), window.innerWidth - b.width - pad);
    box.style.top = top + "px";
    box.style.left = Math.max(pad, left) + "px";
    box.style.visibility = "visible";
  }
  function hide() { current = null; box.classList.remove("on"); }

  document.addEventListener("mouseover", function (e) {
    var el = e.target.closest && e.target.closest(".tip");
    if (el) { if (el !== current) show(el); } else if (current) hide();
  });
  document.addEventListener("focusin", function (e) { if (e.target.classList && e.target.classList.contains("tip")) show(e.target); });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, true);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
})();
