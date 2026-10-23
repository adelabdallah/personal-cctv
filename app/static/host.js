(() => {
  const stack = document.querySelector("[data-camera-stack]");

  document.addEventListener("click", (event) => {
    const slow = event.target.closest("[data-slow-toggle]");
    if (slow && stack && stack.contains(slow)) {
      const tile = slow.closest(".camera-tile");
      if (!tile) {
        return;
      }
      const next = slow.getAttribute("aria-pressed") !== "true";
      fetch(`/cameras/${tile.dataset.cameraId}/slow`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slow: next }),
      })
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
          if (data && typeof data.slow === "boolean") {
            markSlow(slow, data.slow);
          }
        })
        .catch(() => {});
      return;
    }
    const button = event.target.closest("[data-thermal-toggle]");
    if (!button || !stack || !stack.contains(button)) {
      return;
    }
    const tile = button.closest(".camera-tile");
    const pip = tile && tile.querySelector("[data-thermal-pip]");
    if (!tile || !pip) {
      return;
    }
    const show = pip.hidden;
    pip.hidden = !show;
    if (show) {
      pip.src = `/thermal/${tile.dataset.cameraId}`;
      button.textContent = "Hide thermal feed";
    } else {
      pip.removeAttribute("src");
      button.textContent = "Show thermal feed";
    }
  });

  function markSlow(button, on) {
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.textContent = on ? "Slow mode on" : "Slow mode";
  }

  function tileFor(feed) {
    if (!stack) {
      return null;
    }
    let tile = stack.querySelector(`[data-camera-id="${feed.id}"]`);
    if (tile) {
      return tile;
    }
    tile = document.createElement("section");
    tile.className = "camera-tile";
    tile.dataset.cameraId = feed.id;
    tile.innerHTML = `
      <div class="camera-header">
        <h2></h2>
        <div class="camera-meta">
          <button type="button" class="slow-toggle" data-slow-toggle aria-pressed="false">Slow mode</button>
          <button type="button" class="thermal-toggle" data-thermal-toggle hidden>Show thermal feed</button>
          <span class="resolution" hidden></span>
          <span class="offline-note" hidden>Offline</span>
        </div>
      </div>
      <div class="stream-frame">
        <img class="stream" alt="">
        <img class="thermal-pip" data-thermal-pip alt="Thermal feed" hidden>
      </div>`;
    stack.appendChild(tile);
    const image = tile.querySelector(".stream");
    image.src = `/stream/${feed.id}`;
    image.alt = `Live stream for ${feed.name}`;
    return tile;
  }

  function applyFeed(feed) {
    const tile = tileFor(feed);
    if (!tile) {
      return;
    }
    const title = tile.querySelector("h2");
    if (title) {
      title.textContent = feed.name;
    }
    tile.classList.toggle("is-offline", !feed.fresh);
    const note = tile.querySelector(".offline-note");
    if (note) {
      note.hidden = Boolean(feed.fresh);
    }
    const slowButton = tile.querySelector("[data-slow-toggle]");
    if (slowButton) {
      markSlow(slowButton, Boolean(feed.slow));
    }
    const button = tile.querySelector("[data-thermal-toggle]");
    if (button) {
      button.hidden = !feed.thermal;
      if (!feed.thermal) {
        const pip = tile.querySelector("[data-thermal-pip]");
        if (pip) {
          pip.hidden = true;
          pip.removeAttribute("src");
        }
        button.textContent = "Show thermal feed";
      }
    }
    const resolution = tile.querySelector(".resolution");
    if (resolution) {
      if (feed.width) {
        resolution.hidden = false;
        resolution.textContent = `${feed.width}x${feed.height}`;
      } else {
        resolution.hidden = true;
      }
    }
  }

  async function pollFeeds() {
    if (!stack) {
      return;
    }
    try {
      const response = await fetch("/status", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      (payload.feeds || []).forEach(applyFeed);
    } catch (_error) {
      // Keep the tiles already on the page.
    }
  }

  if (stack) {
    setInterval(pollFeeds, 2000);
  }

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
