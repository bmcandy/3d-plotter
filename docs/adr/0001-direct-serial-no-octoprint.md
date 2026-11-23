# Direct USB serial control, no OctoPrint/Klipper middleware

Crealipen needs to stream G-code to the CR-10s. The obvious path for a
Marlin machine is a print host (OctoPrint) or firmware replacement (Klipper),
but a pen never heats, so the entire thermal domain those tools manage is dead
weight, and a second always-on service is more to maintain on the Pi. We
decided the app talks to the printer's USB serial port directly with a minimal
Marlin sender (open port, home, stream lines waiting for `ok`, handle
`busy:`/errors), owning the port exclusively. Consequence: the Sender is our
code and our responsibility, including an emergency stop that lifts the pen.
