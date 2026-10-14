// Номер WhatsApp в международном формате, только цифры
const WHATSAPP_NUMBER = "79635667885";

const burger = document.querySelector(".burger");
const nav = document.getElementById("nav");

burger.addEventListener("click", () => {
  const open = nav.classList.toggle("open");
  burger.setAttribute("aria-expanded", String(open));
});

nav.querySelectorAll("a").forEach((link) =>
  link.addEventListener("click", () => {
    nav.classList.remove("open");
    burger.setAttribute("aria-expanded", "false");
  })
);

const form = document.getElementById("form");

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const msg = document.getElementById("form-msg");
  const { name, contact, goal } = form.elements;

  let valid = true;
  [name, contact].forEach((field) => {
    const empty = !field.value.trim();
    field.classList.toggle("invalid", empty);
    if (empty) valid = false;
  });
  if (!valid) {
    msg.textContent = "Заполните имя и контакт.";
    return;
  }

  const text = [
    "Здравствуйте! Хочу записаться на тренировку.",
    `Имя: ${name.value.trim()}`,
    `Контакт: ${contact.value.trim()}`,
    goal.value.trim() ? `Цель: ${goal.value.trim()}` : "",
  ].filter(Boolean).join("\n");

  window.open(`https://wa.me/${WHATSAPP_NUMBER}?text=${encodeURIComponent(text)}`, "_blank", "noopener");
  msg.textContent = "Открываем WhatsApp — нажмите «Отправить» в чате.";
  form.reset();
});

document.getElementById("year").textContent = new Date().getFullYear();
