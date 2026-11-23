# Standalone containerized app, not a Home Assistant add-on

The app runs on the Pi alongside Home Assistant, which invites packaging it
as an HA add-on or custom integration. Add-ons require the Supervisor/add-on
store machinery and are painful to iterate on; a custom integration is for
controlling HA entities, which isn't the core job. We decided Crealipen is a
standalone web app shipped as a Docker image run via docker compose (serial
device passed through on a stable udev symlink), linked from the HA sidebar.
HA entity/sensor integration (MQTT discovery, automations) is deferred to v2;
Job state in SQLite makes that easy to add later.
