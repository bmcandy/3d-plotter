import React, { useEffect, useRef, useState } from "react";

const EMPTY_SETTINGS = {
  threshold: 128,
  size_mm: 270,
  x: 0,
  y: 0,
  rotation: 0,
  infill: "none",
  spacing: 2.0,
};

export default function App() {
  const [drawing, setDrawing] = useState(null);
  const [settings, setSettings] = useState(EMPTY_SETTINGS);
  const [profile, setProfile] = useState(null);
  const [job, setJob] = useState(null);
  const [busy, setBusy] = useState(false);
  const wsRef = useRef(null);

  useEffect(() => {
    fetch("/api/machine/profile").then((r) => r.json()).then(setProfile);
  }, []);

  const watchJob = (jobId) => {
    if (wsRef.current) wsRef.current.close();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/jobs/${jobId}/ws`);
    wsRef.current = ws;
    let closed = false;
    const finish = (data) => {
      setJob((j) => ({ ...j, ...data }));
      if (["done", "failed", "cancelled"].includes(data.status)) {
        setBusy(false);
        closed = true;
        ws.close();
      }
    };
    // if the websocket dies (e.g. proxy without ws support), fall back to polling
    const poll = async () => {
      while (!closed) {
        try {
          const r = await fetch(`/api/jobs/${jobId}`);
          if (r.ok) finish(await r.json());
        } catch {
          /* backend down; keep trying */
        }
        await new Promise((res) => setTimeout(res, 1000));
      }
    };
    ws.onmessage = (e) => {
      finish(JSON.parse(e.data));
      if (!closed) ws.send("poll");
    };
    ws.onerror = () => {
      if (!closed) {
        closed = true;
        try { ws.close(); } catch { /* already closed */ }
        poll();
      }
    };
    ws.onclose = () => {
      if (!closed) {
        closed = true;
        poll();
      }
    };
  };

  async function upload(e) {
    const file = e.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("image", file);
    const r = await fetch("/api/drawings", { method: "POST", body: form });
    const body = await r.json();
    setDrawing(body);
    setSettings({ ...EMPTY_SETTINGS, ...body.settings });
  }

  async function updateSettings(next) {
    const s = { ...settings, ...next };
    setSettings(s);
    if (!drawing) return;
    const r = await fetch(`/api/drawings/${drawing.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(s),
    });
    const body = await r.json();
    setDrawing(body);
  }

  // mm per image pixel for the current size_mm
  const mmPerPx = drawing ? settings.size_mm / Math.max(drawing.width_px, drawing.height_px) : 0;

  async function fitToBed() {
    if (!drawing || !profile) return;
    const fit = Math.min(profile.bed_width / drawing.width_px, profile.bed_height / drawing.height_px) * 0.9;
    updateSettings({
      size_mm: Math.max(drawing.width_px, drawing.height_px) * fit,
      x: (profile.bed_width - drawing.width_px * fit) / 2,
      y: (profile.bed_height - drawing.height_px * fit) / 2,
    });
  }

  async function plot() {
    setBusy(true);
    await fetch(`/api/drawings/${drawing.id}/gcode`, { method: "POST" });
    const r = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drawing_id: drawing.id }),
    });
    if (r.status === 409) {
      alert("Machine is busy with another job.");
      setBusy(false);
      return;
    }
    const body = await r.json();
    setJob({ id: body.id, status: "pending", progress: 0 });
    watchJob(body.id);
  }

  async function cancel() {
    if (job) await fetch(`/api/jobs/${job.id}/cancel`, { method: "POST" });
  }

  async function estop() {
    await fetch("/api/machine/estop", { method: "POST" });
    setBusy(false);
    if (job) setJob((j) => ({ ...j, status: "cancelled" }));
  }

  async function saveProfile(next) {
    const p = { ...profile, ...next };
    setProfile(p);
    await fetch("/api/machine/profile", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(p),
    });
  }

  // drag on the preview moves the drawing on the bed
  const bedRef = useRef(null);
  const dragRef = useRef(null);
  function onPointerDown(e) {
    if (!drawing) return;
    dragRef.current = { startX: e.clientX, startY: e.clientY, x: settings.x, y: settings.y };
    e.target.setPointerCapture(e.pointerId);
  }
  function onPointerMove(e) {
    if (!dragRef.current || !profile) return;
    const rect = bedRef.current.getBoundingClientRect();
    const bedMmPerScreenPx = profile.bed_width / rect.width;
    const d = dragRef.current;
    setSettings((s) => ({
      ...s,
      x: d.x + (e.clientX - d.startX) * bedMmPerScreenPx,
      y: d.y - (e.clientY - d.startY) * bedMmPerScreenPx,
    }));
  }
  function onPointerUp() {
    if (dragRef.current) {
      dragRef.current = null;
      updateSettings({});
    }
  }

  return (
    <div className="app">
      <header>
        <h1>Crealipen</h1>
        <button className="danger" onClick={estop}>⏹ E-Stop</button>
      </header>
      <main>
        <section className="panel">
          <h2>1. Upload</h2>
          <input type="file" accept="image/*" onChange={upload} />
          {drawing && (
            <h2 style={{ marginTop: "1rem" }}>2. Configure</h2>
          )}
          {drawing && (
            <>
              <label>
                Threshold: {settings.threshold}
                <input type="range" min="1" max="254" value={settings.threshold}
                  onChange={(e) => updateSettings({ threshold: +e.target.value })} />
              </label>
              <label>
                Size (mm): {settings.size_mm}
                <input type="range" min="0" max={profile ? profile.bed_width : 300} step="1" value={settings.size_mm}
                  onChange={(e) => updateSettings({ size_mm: +e.target.value })} />
              </label>
              <label>
                Rotation: {settings.rotation}°
                <input type="range" min="0" max="360" value={settings.rotation}
                  onChange={(e) => updateSettings({ rotation: +e.target.value })} />
              </label>
              <label>
                Infill:
                <select value={settings.infill} onChange={(e) => updateSettings({ infill: e.target.value })}>
                  <option value="none">none</option>
                  <option value="hatch">hatch</option>
                  <option value="crosshatch">crosshatch</option>
                  <option value="concentric">concentric</option>
                </select>
              </label>
              {settings.infill !== "none" && (
                <label>
                  Spacing: {settings.spacing} px
                  <input type="range" min="0.5" max="10" step="0.5" value={settings.spacing}
                    onChange={(e) => updateSettings({ spacing: +e.target.value })} />
                </label>
              )}
              <button className="primary" disabled={busy} onClick={plot}>Plot</button>
              <button onClick={fitToBed} style={{ width: "100%", marginTop: "0.5rem" }}>Fit to bed</button>
            </>
          )}
          {job && (
            <>
              <h2>3. Job</h2>
              <p>
                {job.status} — {job.progress}%
                {busy && <button onClick={cancel}>Cancel</button>}
              </p>
              {job.error && <p className="error">{job.error}</p>}
            </>
          )}
        </section>
        <section className="panel preview" ref={bedRef}
          onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}>
          {drawing ? (
            <>
              <svg
                viewBox={`0 0 ${profile ? profile.bed_width : 300} ${profile ? profile.bed_height : 300}`}
                style={{ background: "#fdf6e3", width: "100%", touchAction: "none" }}
              >
                <g transform={`translate(${settings.x}, ${settings.y}) rotate(${settings.rotation}) scale(${mmPerPx})`}>
                  <g transform={`scale(1, -1) translate(0, -${drawing.height_px})`} dangerouslySetInnerHTML={{ __html: drawing.svg }} />
                </g>
              </svg>
              {!drawing.svg.includes("<path") && (
                <p className="hint">No outlines found — try lowering the threshold.</p>
              )}
            </>
          ) : (
            <p>Upload an image to begin.</p>
          )}
        </section>
        <section className="panel">
          <h2>Machine</h2>
          {profile && (
            <>
              <label>
                Pen-up Z:
                <input type="number" step="0.5" value={profile.pen_up_z}
                  onChange={(e) => saveProfile({ pen_up_z: +e.target.value })} />
              </label>
              <label>
                Pen-down Z:
                <input type="number" step="0.5" value={profile.pen_down_z}
                  onChange={(e) => saveProfile({ pen_down_z: +e.target.value })} />
              </label>
              <label>
                Draw feed:
                <input type="number" step="100" value={profile.draw_feed}
                  onChange={(e) => saveProfile({ draw_feed: +e.target.value })} />
              </label>
              <label>
                Travel feed:
                <input type="number" step="100" value={profile.travel_feed}
                  onChange={(e) => saveProfile({ travel_feed: +e.target.value })} />
              </label>
            </>
          )}
        </section>
      </main>
    </div>
  );
}
