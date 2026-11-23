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

## Deployment (Pi)

1. Create a stable device symlink with a udev rule so the serial device
   doesn't shuffle across reboots. Find the printer's USB ids with
   `lsusb`, then:

   ```sh
   echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="creality"' \
     | sudo tee /etc/udev/rules.d/99-creality.rules
   sudo udevadm control --reload && sudo udevadm trigger
   ```

2. Build and run:

   ```sh
   docker compose up -d --build
   ```

3. Open `http://<pi-ip>:8000`.

### Home Assistant sidebar link

In HA: Settings → Dashboards → Add dashboard → *Webpage link* (iframe),
URL `http://<pi-ip>:8000`. The app trusts the LAN; there is no auth.

## Calibration

Set pen-up Z and pen-down Z in the Machine panel. To find pen-down Z, jog the
pen manually (or use the printer's own controls) until it just touches the
paper, and read the Z off the printer's display.
