#!/bin/bash
cd "$(dirname "$0")"
echo "Starting RicePoster..."
# 127.0.0.1, not localhost: that is the address uvicorn actually binds below,
# and localhost can resolve to ::1 where nothing is listening.
echo "Open http://127.0.0.1:8000 in your browser"
echo ""
# No --reload: it restarts the server on any file change, which would kill an
# in-flight scheduled post mid-run (Phase 2 audit, finding #13). Restart by
# hand while developing.
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
