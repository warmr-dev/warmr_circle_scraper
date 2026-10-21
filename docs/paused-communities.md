# Pausing community reads

Set a connection's priority to `paused` in its community card to exclude its
host from scheduled session scans, public harvest selection and feed polling.
The watcher checks the pause when selecting due work, even if the host already
has a saved session, a membership or an existing watch row. A watch-list refresh
does not override an operator pause.

Stored sessions, posts, leads and feed watermarks are retained. Set the priority
back to normal, low or VIP to resume scheduled reads. A request already in flight
may finish; an explicit manual read is separate from scheduled selection.

No schema migration, historical reclassification or post replay is required.
