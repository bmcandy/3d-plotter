# Crealipen

A web app that turns uploaded images into pen drawings executed on a Creality
CR-10s (pen mounted in place of the extruder) over USB serial.

## Language

### Artifacts

**Drawing**:
A persistent user-created artifact: the original image, its traced SVG, the
settings used, and the generated G-code. A Drawing can be re-plotted any number
of times.
_Avoid_: project, file, artwork

**Trace**:
The conversion of a raster image into vector contour paths. Binary in v1:
threshold to black/white, then trace black regions.
_Avoid_: vectorize, scan

**Infill**:
Filling the enclosed regions of traced contours with a chosen pattern
(parallel-line hatch, cross-hatch, or concentric insets) at a configurable
spacing.
_Avoid_: fill, shading

### Execution

**Job**:
One execution of a Drawing on the machine. A Job has a lifecycle
(pending → running → done/failed/cancelled); at most one Job is active.
_Avoid_: print, task, queue item

**Plot**:
To execute a Drawing's G-code on the machine, producing a pen drawing.
_Avoid_: print, draw (ambiguous with tracing)

**Estimate**:
The predicted duration of a Job, computed from its G-code at constant
velocity (move distance divided by feed rate, no acceleration model). Shown
before plotting and, during a Job, as time remaining.
_Avoid_: ETA, forecast

**Sender**:
The minimal Marlin serial client that streams G-code lines and waits for `ok`
acknowledgements. Owns the serial port exclusively.
_Avoid_: driver, host, print client

### Machine

**Machine Profile**:
The editable set of machine-specific values: bed size, pen-up Z, pen-down Z,
travel speed, and draw speed.
_Avoid_: settings, config (ambiguous)

**Pen Up / Pen Down**:
The two Z positions of the pen mount: Pen Up for travel moves, Pen Down for
drawing moves.
_Avoid_: retract, engage
