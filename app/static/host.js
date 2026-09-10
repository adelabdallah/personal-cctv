(() => {
  const root = document.querySelector("[data-host-status]");
  if (!root) {
    return;
  }

  const hotC = Number(root.dataset.hotC || 70);
  const lowPercent = Number(root.dataset.lowPercent || 20);
  const tempChip = root.querySelector('[data-role="temp"]');
  const batteryChip = root.querySelector('[data-role="battery"]');

  function setChip(el, text, { warn = false, charging = false, title = "" } = {}) {
    if (!el) {
      return;
    }
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      el.classList.remove("is-warn", "is-charging");
      el.removeAttribute("title");
      return;
    }
    el.hidden = false;
    el.textContent = text;
    el.classList.toggle("is-warn", Boolean(warn));
    el.classList.toggle("is-charging", Boolean(charging));
    if (title) {
      el.title = title;
    } else {
      el.removeAttribute("title");
    }
  }

  function apply(data) {
    const temp = data && data.temp_c;
    if (temp == null || Number.isNaN(Number(temp))) {
      setChip(tempChip, "");
    } else {
      const value = Number(temp);
      setChip(tempChip, `${value.toFixed(1)}°C`, { warn: value >= hotC });
    }

    const percent = data && data.battery_percent;
    if (percent == null || Number.isNaN(Number(percent))) {
      setChip(batteryChip, "");
    } else {
      const value = Math.round(Number(percent));
      const charging = Boolean(data.charging);
      setChip(batteryChip, `${value}%`, {
        warn: value <= lowPercent && !charging,
        charging,
        title: charging ? "Charging" : "",
      });
    }
  }

  async function poll() {
    try {
      const response = await fetch("/host", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        return;
      }
      apply(await response.json());
    } catch (_error) {
      // Keep the last rendered values if the poll fails.
    }
  }

  setInterval(poll, 10000);
})();
