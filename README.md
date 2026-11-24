# Crealipen

Turn uploaded images into pen drawings executed on a Creality CR-10s (pen
mounted in place of the extruder) over USB serial. Designed to run on a
Raspberry Pi alongside Home Assistant. See [GLOSSARY.md](./GLOSSARY.md) for
domain language and [docs/adr/](./docs/adr/) for key decisions.

## Layout

- `backend/` — Python FastAPI app (tracing, infill, G-code, Marlin sender, jobs)
- `frontend/` — React UI (upload, preview, configure, plot, monitor)

## Development (x86 machine, no printer needed)

```sh
python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cd backend && ../.venv/bin/python -m pytest
../.venv/bin/python -m app.main            # serves on :8000 with a simulated printer
cd ../frontend && npm install && npm run dev
```

The app runs against a `FakeSerial` simulator in dev; no hardware required.

## Contributing

Work happens on feature branches: branch off `main`, open a pull request, and
merge when CI passes. `main` is protected — direct pushes are not allowed, and
CI must be green before merging. CI runs the backend test suite (Python 3.15)
and builds the frontend on every PR and push to `main`.

## Deployment (Pi, Home Assistant OS)

The Pi runs Home Assistant OS, so Crealipen deploys as an **app** (formerly
add-on) built by the Supervisor from this repo (see
[docs/adr/0004](./docs/adr/0004-ha-app-wrapper-deployment.md)):

1. Push this repo to GitHub (public).
2. In HA: **Settings → Apps → Install app → ⋮ → Repositories**, add the repo
   URL, then install **Crealipen** from the store.
3. In the app's Configuration: `simulate: true` until the CR-10s is plugged
   into the Pi; `serial_port` can be left empty (the first `ttyACM*`/`ttyUSB*`
   device is used automatically).
4. Start the app and enable **Show in sidebar** — Crealipen opens as a native
   panel via ingress.

### Generic Docker hosts (non-HAOS)

The standalone container still works elsewhere:

```sh
docker build -t crealipen .
docker run -d -p 8000:8000 --device /dev/ttyACM0:/dev/creality crealipen
```

Create a stable udev symlink so the device name doesn't shuffle on reboot:

```sh
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="creality"' \
  | sudo tee /etc/udev/rules.d/99-creality.rules
sudo udevadm control --reload && sudo udevadm trigger
```

## Calibration

Set pen-up Z and pen-down Z in the Machine panel. To find pen-down Z, jog the
pen manually (or use the printer's own controls) until it just touches the
paper, and read the Z off the printer's display.
