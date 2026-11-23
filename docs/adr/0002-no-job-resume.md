# No job resume: disconnect fails the Job

Print hosts invest heavily in resuming interrupted prints. For a pen drawing,
resuming mid-path is nearly meaningless — visible seams, double-drawn lines —
and resume logic is a large source of bugs. We decided that if the app
restarts or the serial connection drops mid-Job, the Job is marked failed and
is not resumable; re-plot the whole Drawing. On any reconnect the Sender's
first action is to lift the pen (safe Z). Consequence: the Job state machine
stays small, and "the Job is dead" is always an honest answer.
