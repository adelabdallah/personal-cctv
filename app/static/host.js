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
    if (button && stack && stack.contains(button)) {
      const tile = button.closest(".camera-tile");
      const pip = tile && tile.querySelector("[data-thermal-pip]");
      if (!tile || !pip) {
        return;
      }
      const show = pip.hidden;
      pip.hidden = !show;
      if (!show) {
        const previous = pip.dataset.objectUrl;
        pip.removeAttribute("src");
        if (previous) {
          URL.revokeObjectURL(previous);
          delete pip.dataset.objectUrl;
        }
        button.textContent = "Show thermal feed";
      } else {
        button.textContent = "Hide thermal feed";
      }
      return;
    }
    const frame = event.target.closest("[data-expand]");
    if (!frame || !stack || !stack.contains(frame)) {
      return;
    }
    const tile = frame.closest(".camera-tile");
    if (!tile) {
      return;
    }
    const open = tile.classList.toggle("is-expanded");
    frame.setAttribute("aria-pressed", open ? "true" : "false");
    tile.dataset.paintedAt = "0";
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
      <button type="button" class="stream-frame" data-expand>
        <img class="stream" data-stream alt="">
        <img class="thermal-pip" data-thermal-pip alt="Thermal feed" hidden>
      </button>`;
    stack.appendChild(tile);
    const image = tile.querySelector("[data-stream]");
    image.alt = `Live view for ${feed.name}`;
    const frame = tile.querySelector(".stream-frame");
    if (frame) {
      frame.setAttribute("data-expand", "");
      frame.setAttribute("aria-label", `Enlarge ${feed.name}`);
    }
    layout();
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

  function layout() {
    if (!stack) {
      return;
    }
    const count = stack.querySelectorAll(".camera-tile").length;
    const columns = count >= 3 ? 3 : Math.max(count, 1);
    stack.classList.toggle("is-single", count <= 1);
    stack.style.setProperty("--feed-columns", String(columns));
  }

  async function paint(img, url) {
    if (!img || img.dataset.busy === "1") {
      return;
    }
    img.dataset.busy = "1";
    try {
      const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) {
        return;
      }
      const blob = await response.blob();
      const next = URL.createObjectURL(blob);
      const previous = img.dataset.objectUrl;
      img.src = next;
      img.dataset.objectUrl = next;
      if (previous) {
        URL.revokeObjectURL(previous);
      }
    } catch (_error) {
      // Keep the picture already on screen.
    } finally {
      img.dataset.busy = "0";
    }
  }

  function paintTiles() {
    if (!stack) {
      return;
    }
    const now = Date.now();
    stack.querySelectorAll(".camera-tile").forEach((tile) => {
      const wait = tile.classList.contains("is-expanded") ? 200 : 1000;
      const last = Number(tile.dataset.paintedAt || 0);
      if (now - last < wait) {
        return;
      }
      tile.dataset.paintedAt = String(now);
      paint(tile.querySelector("[data-stream]"), `/snapshot/${tile.dataset.cameraId}`);
      const pip = tile.querySelector("[data-thermal-pip]");
      if (pip && !pip.hidden) {
        paint(pip, `/snapshot/${tile.dataset.cameraId}/thermal`);
      }
    });
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
    layout();
    paintTiles();
    setInterval(pollFeeds, 2000);
    setInterval(paintTiles, 200);
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
