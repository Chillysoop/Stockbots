(function () {
  "use strict";
  const form = document.getElementById("login");
  const err = document.getElementById("err");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.textContent = "";
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Stockbots": "1" },
        body: JSON.stringify({ password: form.elements.password.value }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) location.href = "/";
      else err.textContent = data.error || "Couldn't log in.";
    } catch (e) {
      err.textContent = "Can't reach the server. Try again.";
    }
  });
})();
