const form = document.getElementById("loginForm");
const user = document.getElementById("username");
const pw = document.getElementById("password");
const remember = document.getElementById("remember");
const errorBox = document.getElementById("loginError");
const btn = document.getElementById("loginBtn");

const REMEMBERED = "ta_remembered_username";
const saved = localStorage.getItem(REMEMBERED);
if (saved) {
  user.value = saved;
  remember.checked = true;
  pw.focus();
}

document.getElementById("togglePw").addEventListener("click", (e) => {
  const show = pw.type === "password";
  pw.type = show ? "text" : "password";
  e.target.textContent = show ? "Hide" : "Show";
});

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.classList.remove("hidden");
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.classList.add("hidden");
  if (!user.value.trim() || !pw.value) return showError("Enter your username and password.");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Signing in…';
  try {
    const res = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: user.value.trim(), password: pw.value, remember: remember.checked }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Sign-in failed.");
    if (remember.checked) localStorage.setItem(REMEMBERED, user.value.trim());
    else localStorage.removeItem(REMEMBERED);
    window.location.replace("/");
  } catch (err) {
    showError(err.message);
    pw.value = "";
    pw.focus();
  } finally {
    btn.disabled = false;
    btn.textContent = "Sign in";
  }
});
