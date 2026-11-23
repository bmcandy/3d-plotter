# Deployment vehicle on the Pi: HA app wrapper

ADR 0003 chose a standalone app over an HA add-on for development, but the
production Pi runs Home Assistant OS — a locked-down appliance with no shell
or Docker access, where the only supported way to run custom software is an
app (formerly add-on). We decided to keep the standalone FastAPI app and wrap
it as a locally-built HA app in this repo (`crealipen/`): the Supervisor
builds and manages it, `uart: true` provides the CR-10s serial device (no udev
rule possible inside HAOS, so the port is auto-detected or set per config),
and ingress gives a native sidebar panel. Consequence: the frontend must use
relative asset/API paths everywhere, because ingress serves the panel under
`/ingress/<slug>/`.
