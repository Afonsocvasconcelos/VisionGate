const form = document.getElementById("loginForm");
const password = document.getElementById("password");
const showPassword = document.getElementById("showPassword");
const submit = document.getElementById("submit");
const message = document.getElementById("message");

showPassword.onclick = () => {
  const hidden = password.type === "password";
  password.type = hidden ? "text" : "password";
  showPassword.textContent = hidden ? "Hide" : "Show";
  showPassword.setAttribute("aria-label", hidden ? "Hide password" : "Show password");
};

form.onsubmit = async event => {
  event.preventDefault();
  message.textContent = "";
  submit.disabled = true;
  submit.textContent = "Signing in…";
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username: form.username.value, password: password.value})
    });
    if (!response.ok) {
      const result = await response.json();
      throw new Error(result.detail || "Sign in failed");
    }
    window.location.replace("/");
  } catch (error) {
    password.value = "";
    password.focus();
    message.textContent = error.message;
  } finally {
    submit.disabled = false;
    submit.textContent = "Sign in";
  }
};
