"""Allow ``python -m pipeline`` as a shortcut for ``python -m pipeline.run``."""

from pipeline.run import main

raise SystemExit(main())
