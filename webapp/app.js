const tg = window.Telegram?.WebApp;
if (tg) {
  tg.expand();
}

const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modalTitle");
const modalContent = document.getElementById("modalContent");
const modalClose = document.getElementById("modalClose");

const statusEl = document.getElementById("status");
const plotsEl = document.getElementById("plots");
const storageEl = document.getElementById("storageList");
const marketEl = document.getElementById("marketList");
const seedEl = document.getElementById("seedList");

const actionRaid = document.getElementById("actionRaid");
const actionSabotage = document.getElementById("actionSabotage");
const actionSecurity = document.getElementById("actionSecurity");
const actionSubsidy = document.getElementById("actionSubsidy");

function openModal(title, content) {
  modalTitle.textContent = title;
  modalContent.textContent = content;
  modal.classList.add("open");
}

function closeModal() {
  modal.classList.remove("open");
}

modalClose.addEventListener("click", closeModal);
modal.addEventListener("click", (e) => {
  if (e.target === modal) closeModal();
});

function initPayload() {
  const params = new URLSearchParams(window.location.search);
  const debugUser = params.get("uid");
  return {
    initData: tg?.initData || "",
    debugUser: debugUser ? Number(debugUser) : null,
  };
}

async function api(path, body = {}) {
  const payload = initPayload();
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, ...body }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || "Ошибка сервера");
  }
  return response.json();
}

function renderStatus(state) {
  statusEl.innerHTML = `
    <div><strong>Кредиты:</strong> ${state.player.credits}</div>
    <div><strong>Стамина:</strong> ${state.player.stamina}/${state.player.stamina_max}</div>
    <div><strong>Охрана:</strong> ${state.player.security_level}</div>
    <div><strong>Известность:</strong> ${state.player.notoriety}</div>
    <div><strong>Погода:</strong> ${state.weather.kind}</div>
  `;
}

function renderPlots(state) {
  plotsEl.innerHTML = "";
  state.plots.forEach((plot) => {
    const el = document.createElement("div");
    el.className = "plot";
    let label = `Участок ${plot.plot_id}`;
    let action = null;
    if (plot.status === "empty") {
      label += "\nПусто";
      action = () => openPlantModal(plot.plot_id, state);
    } else if (plot.status === "ready") {
      label += `\n${plot.crop_name}`;
      action = () => harvest(plot.plot_id);
    } else {
      label += `\n${plot.crop_name}`;
      label += `\n~${plot.minutes_left} мин`;
    }
    el.textContent = label;
    if (action) {
      const btn = document.createElement("button");
      btn.className = "btn secondary";
      btn.textContent = plot.status === "empty" ? "Посадить" : "Собрать";
      btn.addEventListener("click", action);
      el.appendChild(btn);
    }
    plotsEl.appendChild(el);
  });
}

function renderStorage(state) {
  storageEl.innerHTML = "";
  if (state.storage.length === 0) {
    storageEl.textContent = "Склад пуст";
    return;
  }
  state.storage.forEach((item) => {
    const el = document.createElement("div");
    el.className = "list-item";
    el.innerHTML = `<span>${item.name}</span><span>${item.qty}</span>`;
    storageEl.appendChild(el);
  });
}

function renderMarket(state) {
  marketEl.innerHTML = "";
  state.market.forEach((item) => {
    const el = document.createElement("div");
    el.className = "list-item";
    el.innerHTML = `<span>${item.name}</span><span>${item.price.toFixed(1)} кр</span>`;
    el.addEventListener("click", () => openSellModal(item.code, item.name, item.price));
    marketEl.appendChild(el);
  });
}

function renderSeeds(state) {
  seedEl.innerHTML = "";
  state.seeds.forEach((item) => {
    const el = document.createElement("div");
    el.className = "list-item";
    el.innerHTML = `<span>${item.name}</span><span>${item.price} кр</span>`;
    el.addEventListener("click", () => openSeedModal(item.code, item.name, item.price));
    seedEl.appendChild(el);
  });
}

async function refresh() {
  try {
    const state = await api("/api/state");
    renderStatus(state);
    renderPlots(state);
    renderStorage(state);
    renderMarket(state);
    renderSeeds(state);
  } catch (err) {
    openModal("Ошибка", err.message);
  }
}

function openSeedModal(code, name, price) {
  modalTitle.textContent = `Купить ${name}`;
  modalContent.innerHTML = `Цена за 1: ${price} кр<br/><br/>`;
  const container = document.createElement("div");
  [1, 5, 10].forEach((qty) => {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = `Купить ${qty}`;
    btn.addEventListener("click", async () => {
      await api("/api/buy_seed", { crop: code, qty });
      closeModal();
      refresh();
    });
    container.appendChild(btn);
  });
  modalContent.appendChild(container);
  modal.classList.add("open");
}

function openSellModal(code, name, price) {
  modalTitle.textContent = `Продать ${name}`;
  modalContent.innerHTML = `Цена: ${price.toFixed(1)} кр<br/><br/>`;
  const container = document.createElement("div");
  [1, 5, 10].forEach((qty) => {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = `Продать ${qty}`;
    btn.addEventListener("click", async () => {
      await api("/api/sell", { crop: code, qty });
      closeModal();
      refresh();
    });
    container.appendChild(btn);
  });
  modalContent.appendChild(container);
  modal.classList.add("open");
}

function openPlantModal(plotId, state) {
  modalTitle.textContent = `Посадить (участок ${plotId})`;
  modalContent.innerHTML = "";
  const container = document.createElement("div");
  state.seeds.forEach((item) => {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = `${item.name} · ${item.grow_min}м`;
    btn.addEventListener("click", async () => {
      await api("/api/plant", { plot_id: plotId, crop: item.code });
      closeModal();
      refresh();
    });
    container.appendChild(btn);
  });
  modalContent.appendChild(container);
  modal.classList.add("open");
}

async function harvest(plotId) {
  await api("/api/harvest", { plot_id: plotId });
  refresh();
}

actionRaid.addEventListener("click", async () => {
  const res = await api("/api/raid");
  openModal("Набег", res.message);
  refresh();
});

actionSabotage.addEventListener("click", async () => {
  const res = await api("/api/sabotage");
  openModal("Саботаж", res.message);
  refresh();
});

actionSecurity.addEventListener("click", async () => {
  const res = await api("/api/security");
  openModal("Охрана", res.message);
  refresh();
});

actionSubsidy.addEventListener("click", async () => {
  const res = await api("/api/subsidy");
  openModal("Субсидия", res.message);
  refresh();
});

refresh();
setInterval(refresh, 30000);
