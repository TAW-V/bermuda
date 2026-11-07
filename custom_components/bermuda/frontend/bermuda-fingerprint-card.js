/*
 * Bermuda fingerprint card (TAW-V fork of Bermuda BLE Trilateration).
 *
 * custom:bermuda-fingerprint-card
 *   Record and manage RSSI fingerprint spots per Home Assistant area.
 *   Plain web component, no build step. Served and auto-loaded by the integration.
 *
 * Options (all optional):
 *   device: sensor.<device>_fingerprint_room   (or any entity of a Bermuda device). Fixes the device and hides
 *           the device dropdown; without it the card lists every device with a fingerprint room sensor and
 *           remembers the choice per browser (localStorage).
 *   duration: 60                                (recording seconds, 30-600)
 *   show_areas_without_spots: true
 *   exclude_areas: [area_id, ...]
 *   title: "Fingerprints"
 *   status_entity / spots_entity: override the global Bermuda sensors if renamed
 */

const CARD_VERSION = "0.8.7-fp7";
const DEFAULT_STATUS = "sensor.bermuda_global_fingerprint_status";
const DEFAULT_SPOTS = "sensor.bermuda_global_fingerprint_spots";
const CONFIRM_MS = 4000;
const STORAGE_KEY = "bermuda-fingerprint-card.device";

const STRINGS = {
  en: {
    title: "Fingerprints",
    unavailable: "Bermuda fingerprinting is not available. Restart Home Assistant after installing the fork.",
    summary: "{spots} spots in {rooms} of {areas} areas",
    fp_room: "Fingerprint room",
    nearest: "Nearest proxy",
    confidence: "confidence",
    fallback: "fallback",
    unknown: "unknown",
    other_floor: "Other",
    no_spots: "no spots yet",
    one_spot: "1 spot",
    n_spots: "{n} spots",
    add: "+ spot",
    add_long: "+ record new spot ({s} s)",
    recording: "Recording {room} · {label}",
    left: "{s} s left · {samples} samples · {scanners} scanners",
    stop: "Stop",
    done: "Saved {room} · {label}: {samples} samples, {scanners} scanners",
    failed: "Recording {room} · {label} failed: the device was not heard",
    cancelled: "Recording {room} · {label} stopped",
    warnings: "note: {w}",
    idle: "Stand still at a spot, then tap + spot for that area.",
    meta: "{samples} samples · {scanners} scanners · {date}",
    rerecord: "Re-record with {device}",
    delete: "Delete",
    clear: "Delete all spots of {room}",
    sure: "Sure?",
    sure_replace: "Replace with {device}?",
    hint: "Re-record replaces that spot's samples with a new recording by {device}; stand at the same place first. + spot adds a new spot and keeps the others.",
    record_with: "Record with",
    this_device: "this device",
    device: "Fixed device (leave empty to choose in the card)",
    duration: "Recording duration (s)",
    show_empty: "Show areas without spots",
    error: "The fingerprint card hit an error: {e}",
  },
  nl: {
    title: "Fingerprints",
    unavailable: "Bermuda-fingerprinting is niet beschikbaar. Herstart Home Assistant na het installeren van de fork.",
    summary: "{spots} locaties in {rooms} van {areas} ruimtes",
    fp_room: "Fingerprint-ruimte",
    nearest: "Dichtstbijzijnde proxy",
    confidence: "zekerheid",
    fallback: "terugval",
    unknown: "onbekend",
    other_floor: "Overig",
    no_spots: "nog geen locaties",
    one_spot: "1 locatie",
    n_spots: "{n} locaties",
    add: "+ locatie",
    add_long: "+ nieuwe locatie opnemen ({s} s)",
    recording: "Opname {room} · {label}",
    left: "nog {s} s · {samples} metingen · {scanners} scanners",
    stop: "Stop",
    done: "Opgeslagen {room} · {label}: {samples} metingen, {scanners} scanners",
    failed: "Opname {room} · {label} mislukt: het apparaat werd niet gehoord",
    cancelled: "Opname {room} · {label} gestopt",
    warnings: "let op: {w}",
    idle: "Ga stil op een plek staan en tik bij die ruimte op + locatie.",
    meta: "{samples} metingen · {scanners} scanners · {date}",
    rerecord: "Opnieuw opnemen met {device}",
    delete: "Verwijderen",
    clear: "Alle locaties van {room} verwijderen",
    sure: "Zeker?",
    sure_replace: "Vervangen door {device}?",
    hint: "Opnieuw opnemen vervangt de metingen van die locatie door een nieuwe opname met {device}; ga eerst op dezelfde plek staan. + locatie voegt een nieuwe locatie toe en laat de andere staan.",
    record_with: "Opnemen met",
    this_device: "dit apparaat",
    device: "Vast apparaat (leeg laten om in de kaart te kiezen)",
    duration: "Opnameduur (s)",
    show_empty: "Ruimtes zonder locaties tonen",
    error: "De fingerprint-kaart liep tegen een fout aan: {e}",
  },
};

function lang(hass) {
  const l = ((hass && (hass.locale && hass.locale.language)) || (hass && hass.language) || "en").toLowerCase();
  return STRINGS[l.split("-")[0]] ? l.split("-")[0] : "en";
}

function t(hass, key, vars) {
  let s = STRINGS[lang(hass)][key] || STRINGS.en[key] || key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(v);
  return s;
}

function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function loadSelected() {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch (_e) {
    return null;
  }
}

function saveSelected(value) {
  try {
    if (value) window.localStorage.setItem(STORAGE_KEY, value);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch (_e) {
    /* private mode or storage disabled: the choice lasts until the page reloads */
  }
}

/** Unnamed iBeacon / IRK devices are called bermuda_<32 hex>_<major>_<minor>: keep the start only. */
function shortName(name) {
  const n = String(name == null ? "" : name);
  const m = n.match(/^(bermuda_)?([0-9a-f]{8})[0-9a-f]{24}/i);
  return m ? `${m[2]}…` : n;
}

/** Display name of a device: the device registry name, else the sensor's friendly name. */
function deviceName(hass, deviceId, entityId) {
  const d = deviceId && hass.devices && hass.devices[deviceId];
  const n = d && (d.name_by_user || d.name);
  if (n) return shortName(n);
  const s = hass.states && hass.states[entityId];
  return shortName((s && s.attributes && s.attributes.friendly_name) || entityId);
}

/** Find the global Bermuda fingerprint entities and the devices with a fingerprint room sensor. */
function findEntities(hass, config, selected) {
  const ents = hass.entities || {};
  const states = hass.states || {};
  const bermuda = Object.keys(ents).filter((id) => ents[id].platform === "bermuda");
  const pick = (cfg, def, suffix) => {
    if (cfg) return cfg;
    if (states[def]) return def;
    return bermuda.find((id) => id.startsWith("sensor.") && id.endsWith(suffix)) || def;
  };
  const status = pick(config.status_entity, DEFAULT_STATUS, "_fingerprint_status");
  const spots = pick(config.spots_entity, DEFAULT_SPOTS, "_fingerprint_spots");

  const rooms = bermuda.filter((id) => id.startsWith("sensor.") && id.endsWith("_fingerprint_room"));
  const seen = new Set();
  const devices = [];
  for (const id of rooms) {
    const devId = ents[id].device_id || null;
    if (devId && seen.has(devId)) continue;
    if (devId) seen.add(devId);
    devices.push({ entity: id, deviceId: devId, name: deviceName(hass, devId, id) });
  }
  devices.sort((a, b) => a.name.localeCompare(b.name));
  let room = null;
  if (config.device) {
    const dev = ents[config.device] && ents[config.device].device_id;
    room = rooms.find((id) => id === config.device) || rooms.find((id) => dev && ents[id].device_id === dev) || null;
  } else if (devices.length >= 1) {
    const pick = selected && devices.find((d) => d.deviceId === selected || d.entity === selected);
    room = pick ? pick.entity : rooms[0]; // default as before fp7: the first fingerprint room sensor
  }
  const deviceId = room && ents[room] ? ents[room].device_id : null;
  const area = deviceId
    ? bermuda.find((id) => id.startsWith("sensor.") && id.endsWith("_area") && ents[id].device_id === deviceId)
    : null;
  // Only pass the device to the service when there is a choice to make.
  const sendDevice = config.device || rooms.length > 1 ? deviceId : null;
  return {
    status,
    spots,
    room,
    area,
    deviceId: sendDevice,
    selectedId: deviceId,
    deviceName: room ? deviceName(hass, deviceId, room) : null,
    devices: config.device ? [] : devices,
  };
}

class BermudaFingerprintCard extends HTMLElement {
  // Every lifecycle method below catches its own errors and shows them inside the card: a throw from
  // setConfig or the hass setter makes Lovelace swap in its generic "Configuration error" card.
  constructor() {
    super();
    this._expanded = new Set();
    this._confirm = null; // {key, until}
    this._registry = null; // {areas, floors}
    this._sig = null;
    this._els = null;
    this._selected = loadSelected(); // device registry id chosen in the dropdown
    this._config = { duration: 60, show_areas_without_spots: true, exclude_areas: [] };
  }

  _root() {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("click", (ev) => {
        try {
          this._onClick(ev);
        } catch (err) {
          this._showError(err);
        }
      });
      this.shadowRoot.addEventListener("change", (ev) => {
        try {
          const el = ev.composedPath().find((n) => n.dataset && n.dataset.select === "device");
          if (!el) return;
          this._selected = el.value || null;
          saveSelected(this._selected);
          this._sig = null;
          this._render();
        } catch (err) {
          this._showError(err);
        }
      });
    }
    return this.shadowRoot;
  }

  connectedCallback() {
    try {
      this._sig = null;
      this._render();
    } catch (err) {
      this._showError(err);
    }
  }

  _showError(err) {
    // eslint-disable-next-line no-console
    console.error("bermuda-fingerprint-card", err);
    try {
      const msg = esc(t(this._hass, "error", { e: (err && err.message) || String(err) }));
      this._sig = null;
      this._root().innerHTML = `<style>${STYLE}</style><ha-card><div class="msg warn">${msg}</div></ha-card>`;
    } catch (_e) {
      /* nothing left to do */
    }
  }

  static getStubConfig() {
    return { duration: 60, show_areas_without_spots: true };
  }

  static getConfigElement() {
    return document.createElement("bermuda-fingerprint-card-editor");
  }

  setConfig(config) {
    try {
      config = config && typeof config === "object" ? config : {};
      const d = Number(config.duration);
      this._config = {
        ...config,
        duration: d >= 30 && d <= 600 ? Math.round(d) : 60,
        show_areas_without_spots: config.show_areas_without_spots !== false,
        exclude_areas: Array.isArray(config.exclude_areas) ? config.exclude_areas : [],
      };
      this._sig = null;
      if (this._hass) this._render();
    } catch (err) {
      this._showError(err);
    }
  }

  set hass(hass) {
    this._hass = hass;
    try {
      if (!hass) return;
      if (!this._hasRegistry(hass) && !this._registry && !this._loading) this._loadRegistry();
      this._render();
    } catch (err) {
      this._showError(err);
    }
  }

  get hass() {
    return this._hass;
  }

  _hasRegistry(hass) {
    return !!(hass && hass.areas && hass.floors && Object.keys(hass.areas).length);
  }

  /** Areas and floors: live from the frontend's hass object when available, else fetched once via websocket. */
  _getRegistry() {
    const hass = this._hass;
    if (this._hasRegistry(hass)) return { areas: Object.values(hass.areas), floors: Object.values(hass.floors) };
    return this._registry || { areas: [], floors: [] };
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  async _loadRegistry() {
    const hass = this._hass;
    if (!hass || typeof hass.callWS !== "function") return;
    this._loading = true;
    try {
      const [areas, floors] = await Promise.all([
        hass.callWS({ type: "config/area_registry/list" }),
        hass.callWS({ type: "config/floor_registry/list" }).catch(() => []),
      ]);
      this._registry = { areas, floors };
    } catch (err) {
      this._registry = { areas: [], floors: [] };
    }
    this._loading = false;
    this._sig = null;
    try {
      this._render();
    } catch (err) {
      this._showError(err);
    }
  }

  _toast(message) {
    this.dispatchEvent(new CustomEvent("hass-notification", { detail: { message }, bubbles: true, composed: true }));
  }

  async _call(service, data) {
    try {
      await this._hass.callService("bermuda", service, data);
    } catch (err) {
      this._toast((err && err.message) || String(err));
    }
  }

  _needsConfirm(key) {
    const now = Date.now();
    if (this._confirm && this._confirm.key === key && this._confirm.until > now) {
      this._confirm = null;
      return false;
    }
    this._confirm = { key, until: now + CONFIRM_MS };
    setTimeout(() => {
      if (this._confirm && this._confirm.key === key && Date.now() >= this._confirm.until) {
        this._confirm = null;
        this._sig = null;
        try {
          this._render();
        } catch (err) {
          this._showError(err);
        }
      }
    }, CONFIRM_MS + 50);
    this._sig = null;
    this._render();
    return true;
  }

  _onClick(ev) {
    const el = ev.composedPath().find((n) => n.dataset && n.dataset.action);
    if (!el) return;
    ev.stopPropagation();
    const { action, area, spot, duration } = el.dataset;
    const dev = this._ents && this._ents.deviceId ? { device: this._ents.deviceId } : {};
    if (action === "toggle") {
      if (this._expanded.has(area)) this._expanded.delete(area);
      else this._expanded.add(area);
      this._sig = null;
      this._render();
    } else if (action === "record") {
      this._call("record_fingerprint", { area_id: area, duration: this._config.duration, ...dev });
    } else if (action === "rerecord") {
      if (this._needsConfirm(`re:${spot}`)) return;
      this._call("record_fingerprint", { spot_id: spot, duration: Number(duration) || this._config.duration, ...dev });
    } else if (action === "delete") {
      if (this._needsConfirm(`del:${spot}`)) return;
      this._call("delete_fingerprint", { spot_id: spot });
    } else if (action === "clear") {
      if (this._needsConfirm(`clr:${area}`)) return;
      this._call("clear_fingerprints", { area_id: area });
    } else if (action === "stop") {
      this._call("cancel_fingerprint", {});
    }
  }

  _render() {
    const hass = this._hass;
    if (!hass || !this._config) return;
    const ents = (this._ents = findEntities(hass, this._config));
    const st = hass.states || {};
    const sig = [st[ents.status], st[ents.spots], st[ents.room], st[ents.area], this._registry, hass.areas, hass.floors,
      hass.entities, hass.devices, ents.room, lang(hass)];
    if (this._sig && sig.every((v, i) => v === this._sig[i])) return;
    this._sig = sig;
    const parts = this._body(ents);
    const root = this._root();
    if (!this._els || !root.contains(this._els.card)) {
      root.innerHTML = `<style>${STYLE}</style><ha-card><div class="p-hdr"></div><div class="p-dev"></div><div class="p-main"></div></ha-card>`;
      const q = (c) => root.querySelector(c);
      this._els = { card: q("ha-card"), hdr: q(".p-hdr"), dev: q(".p-dev"), main: q(".p-main"), html: {} };
    }
    // Only replace the parts that changed, so an open device dropdown survives live sensor updates.
    for (const k of ["hdr", "dev", "main"]) {
      if (this._els.html[k] !== parts[k]) {
        this._els[k].innerHTML = parts[k];
        this._els.html[k] = parts[k];
      }
    }
  }

  _devicePicker(ents) {
    if (!ents.devices || ents.devices.length < 2) return "";
    const opts = ents.devices
      .map((d) => `<option value="${esc(d.deviceId || d.entity)}"${d.entity === ents.room ? " selected" : ""}>${esc(d.name)}</option>`)
      .join("");
    return `<label class="devsel"><ha-icon icon="mdi:cellphone-marker"></ha-icon><span>${esc(t(this._hass, "record_with"))}</span>
      <select data-select="device">${opts}</select></label>`;
  }

  _body(ents) {
    const hass = this._hass;
    const cfg = this._config;
    const states = hass.states || {};
    const spotsState = states[ents.spots];
    const statusState = states[ents.status];
    const title = esc(cfg.title || t(hass, "title"));
    if (!spotsState || !statusState || spotsState.state === "unavailable") {
      return {
        hdr: `<div class="hdr"><ha-icon icon="mdi:fingerprint"></ha-icon><span>${title}</span></div>`,
        dev: "",
        main: `<div class="msg warn">${esc(t(hass, "unavailable"))}</div>`,
      };
    }
    const spotAreas = (spotsState.attributes && spotsState.attributes.areas) || {};
    const reg = this._getRegistry();
    const exclude = new Set(cfg.exclude_areas);
    const areas = (reg.areas || []).filter((a) => a && !exclude.has(a.area_id));
    // Label-only rooms (not HA areas) from the integration
    for (const [key, a] of Object.entries(spotAreas)) {
      if (a && !a.area_id && !exclude.has(key) && a.count) areas.push({ area_id: key, name: a.name || key, floor_id: null, label: true });
    }
    const counted = areas.map((a) => {
      const info = spotAreas[a.area_id] || {};
      return { ...a, name: String(a.name || a.area_id), info: { count: Number(info.count) || 0, spots: Array.isArray(info.spots) ? info.spots : [] } };
    });
    const total = counted.reduce((n, a) => n + a.info.count, 0);
    const withSpots = counted.filter((a) => a.info.count > 0).length;

    const hdr = `<div class="hdr"><ha-icon icon="mdi:fingerprint"></ha-icon><span>${title}</span>
      <span class="sum">${esc(t(hass, "summary", { spots: total, rooms: withSpots, areas: counted.length }))}</span></div>`;
    let html = this._live(ents);
    html += this._status(statusState);

    const floors = (reg.floors || [])
      .filter((f) => f && f.floor_id)
      .map((f) => ({ ...f, name: String(f.name || f.floor_id) }))
      .sort((a, b) => (a.level ?? 0) - (b.level ?? 0) || a.name.localeCompare(b.name));
    const groups = floors.map((f) => ({ name: f.name, icon: f.icon, areas: counted.filter((a) => a.floor_id === f.floor_id) }));
    const known = new Set(floors.map((f) => f.floor_id));
    groups.push({ name: t(hass, "other_floor"), icon: null, areas: counted.filter((a) => !a.floor_id || !known.has(a.floor_id)) });

    const sa = statusState.attributes || {};
    const recArea = statusState.state === "recording" ? sa.area_id || sa.room : null;
    for (const g of groups) {
      const list = g.areas
        .filter((a) => cfg.show_areas_without_spots || a.info.count > 0)
        .sort((a, b) => a.name.localeCompare(b.name));
      if (!list.length) continue;
      html += `<div class="floor">${g.icon ? `<ha-icon icon="${esc(g.icon)}"></ha-icon>` : ""}${esc(g.name)}</div>`;
      for (const a of list) html += this._area(a, recArea === a.area_id, ents);
    }
    return { hdr, dev: this._devicePicker(ents), main: html };
  }

  _live(ents) {
    const hass = this._hass;
    const states = hass.states || {};
    const room = ents.room && states[ents.room];
    if (!room) return "";
    const area = ents.area && states[ents.area];
    const ra = room.attributes || {};
    const conf = Number(ra.confidence || 0);
    const src = ra.source === "fingerprint" ? `${Math.round(conf * 100)}% ${t(hass, "confidence")}` : t(hass, "fallback");
    const val = (s) => (s && !["unknown", "unavailable", ""].includes(s.state) ? s.state : t(hass, "unknown"));
    return `<div class="live">
      <div><div class="k">${esc(t(hass, "fp_room"))}</div><div class="v">${esc(val(room))}</div><div class="s">${esc(src)}</div></div>
      <div><div class="k">${esc(t(hass, "nearest"))}</div><div class="v">${esc(val(area))}</div></div>
    </div>`;
  }

  _status(s) {
    const hass = this._hass;
    const a = s.attributes || {};
    const vars = { room: esc(a.room_name || ""), label: esc(a.label || ""), samples: a.samples || 0, scanners: a.scanners_heard || 0 };
    const by = a.device_name ? ` · ${esc(shortName(a.device_name))}` : "";
    if (s.state === "recording") {
      return `<div class="rec">
        <div class="rt"><ha-icon class="blink" icon="mdi:record-circle"></ha-icon>
          <span>${t(hass, "recording", vars)}${by}</span>
          <button class="btn stop" data-action="stop">${esc(t(hass, "stop"))}</button></div>
        <div class="bar"><div style="width:${Number(a.progress) || 0}%"></div></div>
        <div class="s">${esc(t(hass, "left", { ...vars, s: a.seconds_left ?? "" }))}</div></div>`;
    }
    if (["done", "failed", "cancelled"].includes(s.state)) {
      const warn = Array.isArray(a.warnings) ? a.warnings.join(", ") : a.warnings ? String(a.warnings) : "";
      const w = warn ? ` (${esc(t(hass, "warnings", { w: warn }))})` : "";
      const cls = s.state === "done" ? "ok" : "warn";
      return `<div class="msg ${cls}">${t(hass, s.state, vars)}${by}${w}</div>`;
    }
    return `<div class="msg">${esc(t(hass, "idle"))}</div>`;
  }

  _area(a, recording, ents) {
    const hass = this._hass;
    const devVars = { device: esc((ents && ents.deviceName) || t(hass, "this_device")) };
    const n = a.info.count;
    const cls = n >= 5 ? "good" : n > 0 ? "some" : "none";
    const label = n === 0 ? t(hass, "no_spots") : n === 1 ? t(hass, "one_spot") : t(hass, "n_spots", { n });
    const open = this._expanded.has(a.area_id);
    const id = esc(a.area_id);
    let html = `<div class="area ${recording ? "recording" : ""}">
      <div class="row" data-action="toggle" data-area="${id}">
        <ha-icon class="ai ${cls}" icon="${esc(a.icon || (a.label ? "mdi:tag-outline" : "mdi:door"))}"></ha-icon>
        <div class="an"><div class="v">${esc(a.name)}</div><div class="s"><span class="chip ${cls}">${esc(label)}</span></div></div>
        <ha-icon class="chev" icon="${open ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon>
        <button class="btn add" data-action="record" data-area="${id}">${esc(t(hass, "add"))}</button>
      </div>`;
    if (open) {
      html += `<div class="spots">`;
      for (const s of a.info.spots) {
        if (!s) continue;
        const date = String(s.updated || s.created || "").slice(5, 16).replace("T", " ");
        const reKey = this._confirm && this._confirm.key === `re:${s.id}`;
        const delKey = this._confirm && this._confirm.key === `del:${s.id}`;
        const mine = ents && s.device_id && s.device_id === ents.selectedId;
        const by = s.device ? `<span class="chip dev ${mine ? "mine" : ""}">${esc(shortName(s.device))}</span> ` : "";
        html += `<div class="spot"><ha-icon icon="mdi:map-marker"></ha-icon>
          <div class="an"><div class="v">${esc(s.label)}</div>
            <div class="s">${by}${esc(t(hass, "meta", { samples: s.samples, scanners: s.scanners, date }))}</div></div>
          <button class="ib ${reKey ? "confirm" : ""}" title="${t(hass, "rerecord", devVars)}" data-action="rerecord"
            data-spot="${esc(s.id)}" data-duration="${esc(s.duration_s || "")}">${reKey ? t(hass, "sure_replace", devVars) : '<ha-icon icon="mdi:refresh"></ha-icon>'}</button>
          <button class="ib del ${delKey ? "confirm" : ""}" title="${esc(t(hass, "delete"))}" data-action="delete"
            data-spot="${esc(s.id)}">${delKey ? esc(t(hass, "sure")) : '<ha-icon icon="mdi:delete"></ha-icon>'}</button></div>`;
      }
      html += `<button class="btn wide" data-action="record" data-area="${id}">${esc(t(hass, "add_long", { s: this._config.duration }))}</button>`;
      if (n > 0) {
        const clr = this._confirm && this._confirm.key === `clr:${a.area_id}`;
        html += `<div class="s hint">${t(hass, "hint", devVars)}</div>
          <button class="btn wide danger ${clr ? "confirm" : ""}" data-action="clear" data-area="${id}">
          ${clr ? esc(t(hass, "sure")) : esc(t(hass, "clear", { room: a.name }))}</button>`;
      }
      html += `</div>`;
    }
    return html + `</div>`;
  }
}

const STYLE = `
  ha-card { padding: 12px 12px 8px; }
  .hdr { display: flex; align-items: center; gap: 8px; font-size: 1.2em; font-weight: 500; flex-wrap: wrap; }
  .hdr .sum { font-size: 0.75em; font-weight: 400; color: var(--secondary-text-color); margin-left: auto; }
  .live { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 10px 0; }
  .live > div { background: var(--secondary-background-color); border-radius: 12px; padding: 8px 10px; }
  .k, .s { font-size: 0.8em; color: var(--secondary-text-color); }
  .v { font-weight: 500; }
  .msg { margin: 8px 0; padding: 8px 10px; border-radius: 12px; background: var(--secondary-background-color); font-size: 0.9em; }
  .msg.ok { border-left: 4px solid var(--success-color, #4caf50); }
  .msg.warn { border-left: 4px solid var(--warning-color, #ff9800); }
  .rec { margin: 8px 0; padding: 8px 10px; border-radius: 12px; background: var(--secondary-background-color);
    border-left: 4px solid var(--error-color, #db4437); }
  .rt { display: flex; align-items: center; gap: 8px; font-weight: 500; }
  .rt span { flex: 1; }
  .bar { height: 8px; border-radius: 4px; background: var(--divider-color); overflow: hidden; margin: 8px 0 4px; }
  .bar > div { height: 100%; background: var(--primary-color); transition: width 1s linear; }
  .blink { color: var(--error-color, #db4437); animation: blink 1.4s ease-in-out infinite; }
  @keyframes blink { 50% { opacity: 0.3; } }
  .floor { display: flex; align-items: center; gap: 6px; margin: 14px 2px 4px; font-weight: 500;
    color: var(--secondary-text-color); text-transform: uppercase; font-size: 0.8em; letter-spacing: 0.05em; }
  .floor ha-icon { --mdc-icon-size: 18px; }
  .area { border-radius: 12px; background: var(--card-background-color); border: 1px solid var(--divider-color); margin: 6px 0; }
  .area.recording { border-color: var(--error-color, #db4437); }
  .row { display: flex; align-items: center; gap: 10px; padding: 8px 8px 8px 10px; cursor: pointer; min-height: 44px; }
  .an { flex: 1; min-width: 0; }
  .an .v { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ai.good { color: var(--success-color, #4caf50); }
  .ai.some { color: var(--warning-color, #ff9800); }
  .ai.none { color: var(--disabled-text-color, #9e9e9e); }
  .chip { display: inline-block; padding: 0 8px; border-radius: 10px; font-size: 0.95em; line-height: 18px; }
  .chip.good { background: color-mix(in srgb, var(--success-color, #4caf50) 20%, transparent); }
  .chip.some { background: color-mix(in srgb, var(--warning-color, #ff9800) 20%, transparent); }
  .chip.none { background: var(--secondary-background-color); }
  .chev { color: var(--secondary-text-color); }
  .btn { font: inherit; font-size: 0.9em; font-weight: 500; border: none; border-radius: 18px; padding: 0 14px; min-height: 36px;
    background: var(--primary-color); color: var(--text-primary-color, #fff); cursor: pointer; white-space: nowrap; }
  .btn.stop { background: var(--error-color, #db4437); }
  .btn.wide { display: block; width: 100%; margin: 8px 0 4px; }
  .btn.danger { background: transparent; color: var(--error-color, #db4437); border: 1px solid var(--error-color, #db4437); }
  .btn.confirm, .ib.confirm { background: var(--error-color, #db4437); color: #fff; }
  .spots { padding: 0 10px 8px; border-top: 1px solid var(--divider-color); }
  .spot { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--divider-color); }
  .spot > ha-icon { color: var(--secondary-text-color); --mdc-icon-size: 20px; }
  .ib { border: none; background: var(--secondary-background-color); color: var(--primary-text-color); border-radius: 50%;
    width: 40px; height: 40px; min-width: 40px; cursor: pointer; font: inherit; font-size: 0.75em; padding: 0; }
  .ib.del { color: var(--error-color, #db4437); }
  .ib.confirm { border-radius: 20px; width: auto; padding: 0 10px; }
  .hint { margin-top: 6px; }
  .devsel { display: flex; align-items: center; gap: 8px; margin: 10px 0 0; font-size: 0.9em; color: var(--secondary-text-color); }
  .devsel ha-icon { --mdc-icon-size: 20px; }
  .devsel select { flex: 1; min-width: 0; font: inherit; font-size: 1.05em; color: var(--primary-text-color);
    background: var(--secondary-background-color); border: 1px solid var(--divider-color); border-radius: 12px;
    padding: 6px 10px; min-height: 36px; cursor: pointer; }
  .chip.dev { background: var(--secondary-background-color); border: 1px solid var(--divider-color); font-size: 0.9em; }
  .chip.dev.mine { border-color: var(--primary-color); color: var(--primary-color); }
`;

class BermudaFingerprintCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...(config || {}) };
    try {
      this._update();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("bermuda-fingerprint-card-editor", err);
    }
  }

  set hass(hass) {
    this._hass = hass;
    try {
      this._update();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("bermuda-fingerprint-card-editor", err);
    }
  }

  _update() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => t(this._hass, s.label_key);
      this._form.addEventListener("value-changed", (ev) => {
        const config = { ...ev.detail.value };
        for (const k of Object.keys(config)) if (config[k] === "" || config[k] === undefined) delete config[k];
        this._config = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = { duration: 60, show_areas_without_spots: true, ...this._config };
    this._form.schema = [
      { name: "device", label_key: "device", selector: { entity: { integration: "bermuda", domain: "sensor" } } },
      { name: "duration", label_key: "duration", selector: { number: { min: 30, max: 600, step: 10, unit_of_measurement: "s", mode: "box" } } },
      { name: "show_areas_without_spots", label_key: "show_empty", selector: { boolean: {} } },
    ];
  }
}

// Each element is guarded on its own, so loading this file twice (or next to an older copy) never throws.
try {
  if (!customElements.get("bermuda-fingerprint-card")) {
    customElements.define("bermuda-fingerprint-card", BermudaFingerprintCard);
    // eslint-disable-next-line no-console
    console.info(`%c BERMUDA-FINGERPRINT-CARD %c ${CARD_VERSION} `, "background:#3f51b5;color:#fff", "");
  }
  if (!customElements.get("bermuda-fingerprint-card-editor")) {
    customElements.define("bermuda-fingerprint-card-editor", BermudaFingerprintCardEditor);
  }
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c && c.type === "bermuda-fingerprint-card")) {
    window.customCards.push({
      type: "bermuda-fingerprint-card",
      name: "Bermuda fingerprint card",
      description: "Record and manage Bermuda RSSI fingerprint spots per area.",
      preview: false,
      documentationURL: "https://github.com/TAW-V/bermuda",
    });
  }
} catch (err) {
  // eslint-disable-next-line no-console
  console.error("bermuda-fingerprint-card: could not register", err);
}
