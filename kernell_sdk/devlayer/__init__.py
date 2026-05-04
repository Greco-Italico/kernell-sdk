"""
Kernell OS — DevLayer
═════════════════════
The developer-facing interface to the Kernell distributed execution fabric.

Unlike Cursor (single model, local execution, no verification),
the DevLayer routes coding tasks through a marketplace of competing agents,
returns cryptographically verified results with visual diffs, and lets the
developer accept/reject with a single keystroke.

Components:
  - ContextRouter:  Indexes the codebase and selects relevant context
  - TaskClient:     Submits tasks to the Kernell network and tracks execution
  - PreviewEngine:  Renders diffs, receipts, and agent reputation for review
  - CLI (kernell dev): Developer-facing commands
"""
__version__ = "0.1.0"
